"""Bounded JSONL ingestion and safe, no-overwrite snapshot files."""

import json
from pathlib import Path

from .core import MAX_STATE_BYTES, Sketch

MAX_LINE = 4096
MAX_INPUT = 64 * 1024 * 1024
MAX_RECORDS = 1_000_000


def strict_json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON field")
            result[key] = value
        return result

    def constant(_):
        raise ValueError("non-finite JSON constant")

    return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)


def build(path, **config):
    sketch = Sketch(**config)
    size = records = 0
    with Path(path).open("rb") as source:
        while line := source.readline(MAX_LINE + 1):
            size += len(line)
            records += 1
            if len(line) > MAX_LINE or size > MAX_INPUT or records > MAX_RECORDS:
                raise ValueError("JSONL byte, line, or record limit exceeded")
            row = strict_json(line.decode("utf-8"))
            if type(row) is not dict or set(row) != {"key", "count"}:
                raise ValueError("each JSONL record needs exactly key and count")
            sketch.update(row["key"], row["count"])
    return sketch


def load(path):
    with Path(path).open("rb") as source:
        data = source.read(MAX_STATE_BYTES + 1)
    return Sketch.loads(data)


def save(sketch, path):
    data = sketch.dumps()
    with Path(path).open("xb") as destination:
        destination.write(data)
