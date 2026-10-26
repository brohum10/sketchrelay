# SketchRelay

[![Checks](https://github.com/brohum10/sketchrelay/actions/workflows/ci.yml/badge.svg)](https://github.com/brohum10/sketchrelay/actions/workflows/ci.yml)

**Combine telemetry from several workers without shipping every observed label.**

SketchRelay maintains approximate label frequencies with a Count-Min sketch and
approximate distinct-label counts with HyperLogLog. Workers can serialize their
summaries and merge compatible states into the exact same sketch state a single
worker would have produced. The default snapshot is **66,620 bytes**, independent
of how many labels have been observed, until counter/estimator limits apply.

Python 3.12+ · standard library only · portable binary state · MIT

## Run the reproducible demo

From a cloned repository:

```sh
python scripts/demo.py
python scripts/demo.py --verify
python -m unittest discover -s tests -v
```

Use `python3` if that is your Python 3.12+ command. No account, API key, server,
or dependency installation is needed for these commands. The demo generates
100,000 synthetic observations, distributes them over eight workers, compares
the merged binary state against a single pass, and checks estimates against an
exact `Counter` retained **only in the verification harness**.

### Results on the checked-in fixture

| Query label | Exact frequency | Estimate |
| --- | ---: | ---: |
| `/checkout` | 29,995 | 30,004 |
| `/search` | 22,440 | 22,453 |
| `route-12345` | 1 | 8 |
| `never-observed` | 0 | 8 |

The distinct-label count is 25,004; HyperLogLog estimates 25,846.735
(**+3.37% relative error**). All 25,004 observed-label frequency queries avoid
underestimation on this fixture; their maximum overestimate is 17.

These results deliberately include poor low-frequency estimates and an absent
label with a nonzero estimate. **A frequency estimate is not a membership test.**
This is useful for approximate telemetry, not exact billing, access control,
rare-event alerts, or an event ledger.

The byte count includes the binary header, counters, registers, and checksum.
It is **serialized size, not total Python RAM**, and is not compared against a
measured exact-counter memory footprint. No throughput or production savings
claim is made. Full counters, query errors, cardinality error, and state SHA-256
are in [evidence/synthetic.json](evidence/synthetic.json). Verification checks
integers/state identity exactly and floats with `rtol=atol=1e-10` tolerance.

## Try two worker files

Choose **new output filenames** in a writable directory:

```sh
python -m sketchrelay build examples/worker-a.jsonl --output worker-a.skr
python -m sketchrelay build examples/worker-b.jsonl --output worker-b.skr
python -m sketchrelay merge worker-a.skr worker-b.skr --output combined.skr
python -m sketchrelay query combined.skr /checkout /search /missing
python -m sketchrelay inspect combined.skr
```

The combined fixture has total mass 34 and estimates `/checkout` at 20. Each
JSONL line must have exactly a nonempty `key` string and positive integer `count`:

```json
{"key":"/checkout","count":12}
```

Weights add to frequency mass but do not multiply distinct cardinality. Two
records for one label with counts 10 and 20 mean frequency mass 30 and one
distinct observed label. Keys are case-sensitive UTF-8 bytes with no implicit
Unicode normalization; normalize them consistently upstream if desired.

Optional installation: `python -m pip install .`, then use `sketchrelay` instead
of `python -m sketchrelay`. Installation may download build tooling; the runtime
uses no third-party packages. Success emits JSON and exits 0. Invalid input,
corrupt state, incompatible merges, or I/O errors emit stderr and exit 2.

## How merging works

```text
worker A -> frequency counters + cardinality registers ---\
worker B -> frequency counters + cardinality registers ----> compatible merge
worker C -> frequency counters + cardinality registers ---/  -> compact global sketch
```

**Count-Min:** each nonnegative weighted update increments one bucket in every
row. A query takes the minimum of those row counters. Collisions can increase
an estimate but cannot make it smaller than the true frequency for a genuine
nonnegative stream. Merge adds matching counters and exact total masses.

**HyperLogLog:** a domain-separated 64-bit hash chooses a register from its
leading precision bits. The remaining bits determine a leading-zero rank; a
register retains the largest observed rank. Estimation uses the standard
harmonic estimator with a small-range linear-counting correction. Merge takes
the register-wise maximum. No large-range bias correction near 64-bit hash
saturation is implemented; estimates in that unsupported range are rejected.

The default shape is 4 × 2,048 64-bit frequency counters plus 1,024 one-byte HLL
registers. SHA-256 framing includes algorithm domain, unsigned 64-bit seed,
and (for Count-Min) row index. Seed and dimensions are part of compatibility.
There is no claimed probabilistic error ceiling for each individual stream,
adversarial robustness guarantee, or per-query confidence interval.

The merge operation is associative and commutative for compatible valid states,
subject to the mass limit. It returns a new object without mutating either input.
Compatible-state merge equals ingesting the **multiset union** of input streams.

### Important: merging is not retry-idempotent

Merging the same worker snapshot twice **doubles its frequency contribution**.
The HLL registers remain unchanged because they describe a set of labels. There
is no shard-delivery ledger, sequence number, window expiry, or automatic overlap
detection. Deduplicate deliveries and choose disjoint stream partitions upstream.

Sketches do not enumerate their keys or discover top-K labels. Query labels must
come from the caller; the hot-label queries in the demo are known in advance.
The tool is not a log store or a complete monitoring platform.

## Binary format and safety

The versioned format uses a fixed big-endian header (`SKRLY001`, width, depth,
precision, seed, total), big-endian 64-bit counters, one-byte registers, and a
32-byte SHA-256 checksum. The loader validates size, version, dimensions, row
sums, register ranges, and basic total/register consistency before accepting
state. Snapshots never use pickle or executable expressions.

A checksum detects accidental corruption, **not malicious forgery**. A caller
can recompute it; structural validation cannot prove a state came from a real
stream. Summaries are also **not anonymous or privacy-preserving**: known labels
can be queried and guessed, and query output repeats them. See [SECURITY.md](SECURITY.md).

`build` reads one bounded JSONL record at a time. Defaults/configuration limits:

- Width: power of two, 32–8,192; depth: 1–8; HLL precision: 7–14.
- Positive counts only; exact total mass must fit signed 64 bits. Overflow is
  rejected before mutation, including during merges. Deletions are unsupported.
- Keys: 1–512 UTF-8 bytes. Blank lines, unknown/duplicate JSON fields, invalid
  encodings, floating/bool counts, and non-finite constants are rejected.
- JSONL input: at most 4,096 bytes per line, 64 MiB total, one million records.
- Snapshot input: at most one million bytes. A CLI merge accepts 2–32 snapshots;
  query accepts at most 100 keys. API calls may merge repeatedly.

Output files are opened exclusively: an existing path is never overwritten.
Malformed input and incompatible merges fail before creating output. An I/O
failure **during a new file's write** may leave a partial new file; it is not an
atomic/durable publication protocol. The checksum lets a later load reject a
truncated snapshot. Inspect partial files and choose a new output name on retry.

## Tests and scope

The 34 tests cover 100 seeded weighted streams checked against exact frequency
counters, merge-versus-single-pass byte equality, all three-way merge orders,
an independent bit-string oracle for HLL register ranks, overflow atomicity,
Unicode semantics, portable encoding, checksum failures, forged structural
states, overwrite refusal, and end-to-end CLI behavior. CI repeats the suite,
100,000-record evidence verification, and package installation on Linux/Windows
with Python 3.12/3.14. These checks are not a formal proof or statistical
validation over arbitrary workloads.

The algorithms are established work: [Cormode and Muthukrishnan's Count-Min sketch](https://dimacs.rutgers.edu/~graham/pubs/papers/cm-full.pdf)
and [Flajolet et al.'s HyperLogLog](https://algo.inria.fr/flajolet/Publications/FlFuGaMe07.pdf).
This is an independent learning implementation, not a new sketching algorithm,
HLL++, a distributed service, or a production telemetry guarantee. Built with
AI assistance.
