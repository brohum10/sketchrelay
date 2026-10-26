import argparse
import json
from pathlib import Path
import sys

from .io import build, load, save


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Build and merge approximate stream frequency/cardinality summaries."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    ingest = commands.add_parser("build")
    ingest.add_argument("events", type=Path)
    ingest.add_argument("--output", type=Path, required=True)
    for name, default in (("width", 2048), ("depth", 4), ("precision", 10), ("seed", 0)):
        ingest.add_argument("--" + name, type=int, default=default)
    combine = commands.add_parser("merge")
    combine.add_argument("snapshots", nargs="+", type=Path)
    combine.add_argument("--output", type=Path, required=True)
    query = commands.add_parser("query")
    query.add_argument("snapshot", type=Path)
    query.add_argument("keys", nargs="+")
    inspect = commands.add_parser("inspect")
    inspect.add_argument("snapshot", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            sketch = build(
                args.events,
                width=args.width,
                depth=args.depth,
                precision=args.precision,
                seed=args.seed,
            )
            result = sketch.summary()
            save(sketch, args.output)
        elif args.command == "merge":
            if not 2 <= len(args.snapshots) <= 32:
                raise ValueError("merge needs 2-32 snapshots")
            sketch = load(args.snapshots[0])
            for path in args.snapshots[1:]:
                sketch = sketch.merge(load(path))
            result = sketch.summary()
            save(sketch, args.output)
        else:
            sketch = load(args.snapshot)
            result = sketch.summary()
            if args.command == "query":
                if len(args.keys) > 100:
                    raise ValueError("at most 100 query keys")
                result["frequency_estimates"] = {
                    key: sketch.estimate(key) for key in sorted(set(args.keys))
                }
        print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
        return 0
    except (OSError, ValueError, OverflowError, RecursionError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
