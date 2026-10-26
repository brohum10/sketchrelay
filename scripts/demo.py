"""Seeded synthetic telemetry; exact counts exist only in this verification harness."""

from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import random
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sketchrelay import Sketch


def reproduce():
    rng = random.Random(20261026)
    events = [f"route-{i:05d}" for i in range(25000)]
    events += rng.choices(
        ["/checkout", "/search", "/login", "/catalog"], weights=[4, 3, 2, 1], k=75000
    )
    rng.shuffle(events)
    exact = Counter(events)
    one_pass = Sketch(seed=20261026)
    shards = [Sketch(seed=20261026) for _ in range(8)]
    for i, key in enumerate(events):
        one_pass.update(key)
        shards[i % len(shards)].update(key)
    merged = Sketch(seed=20261026)
    for shard in reversed(shards):
        merged = merged.merge(Sketch.loads(shard.dumps()))
    if merged.dumps() != one_pass.dumps():
        raise AssertionError("shard merge differs from one-pass state")
    estimates = {key: merged.estimate(key) for key in exact}
    if any(estimates[key] < actual for key, actual in exact.items()):
        raise AssertionError("Count-Min underestimated a nonnegative frequency")
    queries = ["/checkout", "/search", "/login", "/catalog", "route-12345", "never-observed"]
    cardinality = merged.distinct()
    return dict(
        corpus=dict(
            seed=20261026,
            records=len(events),
            distinct_labels=len(exact),
            shards=8,
            unique_tail_labels=25000,
            weighted_hot_draws=75000,
        ),
        summary=merged.summary(),
        merge_matches_one_pass_bytes=True,
        queries=[
            dict(
                key=key,
                actual=exact[key],
                estimated=merged.estimate(key),
                overestimate=merged.estimate(key) - exact[key],
            )
            for key in queries
        ],
        all_known_labels=dict(
            checked=len(exact),
            underestimates=0,
            mean_overestimate=math.fsum(estimates[key] - exact[key] for key in exact) / len(exact),
            max_overestimate=max(estimates[key] - exact[key] for key in exact),
        ),
        cardinality=dict(
            actual=len(exact),
            estimated=cardinality,
            relative_error=(cardinality - len(exact)) / len(exact),
        ),
        state_sha256=hashlib.sha256(merged.dumps()).hexdigest(),
    )


def compare(expected, actual, path="report"):
    if type(expected) is float:
        if type(actual) not in (float, int) or not math.isclose(
            expected, actual, rel_tol=1e-10, abs_tol=1e-10
        ):
            raise AssertionError(f"numeric mismatch at {path}")
    elif type(expected) is dict:
        if type(actual) is not dict or set(expected) != set(actual):
            raise AssertionError(f"field mismatch at {path}")
        for key in expected:
            compare(expected[key], actual[key], f"{path}.{key}")
    elif type(expected) is list:
        if type(actual) is not list or len(expected) != len(actual):
            raise AssertionError(f"list mismatch at {path}")
        for i, (a, b) in enumerate(zip(expected, actual)):
            compare(a, b, f"{path}[{i}]")
    elif type(expected) is not type(actual) or expected != actual:
        raise AssertionError(f"value mismatch at {path}")


def main():
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    result = reproduce()
    if args.verify:
        compare(json.loads((ROOT / "evidence/synthetic.json").read_text(encoding="utf-8")), result)
        print(
            "Verified 100000 synthetic records, all 25004 frequency queries, shard merge bytes, and cardinality evidence."
        )
    else:
        print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
