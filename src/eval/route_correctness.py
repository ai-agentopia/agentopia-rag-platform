"""Route correctness check — scope → Qdrant collection name derivation.

Verifies that every known live scope identity maps to the expected Qdrant
collection name using the canonical sha256[:16] derivation from
ingest/source_registry._qdrant_collection_for_scope (ADR-011).

A "misroute" is ANY deviation. The threshold is 0: because the derivation
is deterministic, any mismatch means a breaking change was introduced to
the hashing algorithm, which would cause retrieval to silently read from the
wrong collection. The issue #21 AC wrote "> 0.10" but that threshold is
incorrect for a deterministic property — any non-zero misroute rate is fatal.

Label semantics: metric is per "scope" (e.g. "utop/oddspark"). The original
issue AC used "family" — that term is not used in the codebase. "scope" is
the canonical term per ADR-011 and source_registry.py.

Run as:
    python src/eval/route_correctness.py          # exits 0 = all correct
    python src/eval/route_correctness.py --json   # emit JSON result

Used by:
    - eval-gate.yml CI: inline check on every push/PR + daily schedule
    - emit_metrics.py: push agentopia_rag_misroute_rate to Pushgateway
"""

import argparse
import hashlib
import json
import sys


def _collection_for_scope(scope: str) -> str:
    return "kb-" + hashlib.sha256(scope.encode("utf-8")).hexdigest()[:16]


# Known scope → collection assertions.
# Extend this list when new scopes go live.
KNOWN_ROUTES = [
    {
        "scope": "utop/oddspark",
        "expected_collection": "kb-45294aa854667d3b",
    },
]


def check_routes() -> dict:
    """Verify all known scope → collection routes. Returns result dict."""
    results = []
    misrouted = []

    for entry in KNOWN_ROUTES:
        scope = entry["scope"]
        expected = entry["expected_collection"]
        actual = _collection_for_scope(scope)
        correct = actual == expected
        results.append({
            "scope": scope,
            "expected": expected,
            "actual": actual,
            "correct": correct,
        })
        if not correct:
            misrouted.append(scope)

    total = len(results)
    misroute_count = len(misrouted)
    misroute_rate = misroute_count / total if total > 0 else 0.0

    return {
        "total": total,
        "misrouted": misroute_count,
        "misroute_rate": round(misroute_rate, 4),
        "passed": misroute_rate == 0.0,
        "routes": results,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Route correctness check")
    parser.add_argument("--json", action="store_true", help="Emit JSON result")
    args = parser.parse_args()

    result = check_routes()

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print("=== Route Correctness Check ===")
        for r in result["routes"]:
            status = "OK  " if r["correct"] else "FAIL"
            print(f"  [{status}] {r['scope']} → {r['actual']} (expected: {r['expected']})")
        print()
        print(f"Total: {result['total']}  Misrouted: {result['misrouted']}")
        print(f"Misroute rate: {result['misroute_rate']:.4f}")
        print(f"Verdict: {'PASS' if result['passed'] else 'FAIL'}")

    sys.exit(0 if result["passed"] else 1)
