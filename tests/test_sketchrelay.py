from collections import Counter
import hashlib
import itertools
import json
import math
from pathlib import Path
import random
import struct
import subprocess
import sys
import tempfile
import unittest

from sketchrelay import Sketch
from sketchrelay.core import HEADER, MAGIC, MAX_COUNT, MAX_STATE_BYTES
from sketchrelay.io import build, load, save

ROOT = Path(__file__).resolve().parents[1]


def rechecksum(body):
    return bytes(body) + hashlib.sha256(body).digest()


class CoreTests(unittest.TestCase):
    def test_empty(self):
        sketch = Sketch()
        self.assertEqual(sketch.estimate("absent"), 0)
        self.assertEqual(sketch.distinct(), 0)
        self.assertEqual(sketch.total, 0)

    def test_weighted_frequencies(self):
        sketch = Sketch()
        sketch.update("checkout", 5)
        sketch.update("checkout", 7)
        sketch.update("search", 3)
        self.assertEqual(sketch.total, 15)
        self.assertEqual(sketch.estimate("checkout"), 12)
        self.assertEqual(sketch.estimate("search"), 3)

    def test_weights_do_not_multiply_cardinality(self):
        sketch = Sketch()
        sketch.update("one", 1000)
        before = sketch.distinct()
        sketch.update("one", 1000)
        self.assertEqual(before, sketch.distinct())
        self.assertAlmostEqual(before, 1, delta=0.01)

    def test_invalid_updates_are_atomic(self):
        sketch = Sketch()
        sketch.update("valid", 2)
        original = sketch.dumps()
        for key, count in (
            ("", 1),
            ("x" * 513, 1),
            (None, 1),
            ("key", 0),
            ("key", -1),
            ("key", True),
            ("key", 1.0),
            ("\ud800", 1),
        ):
            with self.subTest(key=repr(key), count=count), self.assertRaises(ValueError):
                sketch.update(key, count)
            self.assertEqual(sketch.dumps(), original)

    def test_utf8_byte_limit_not_character_count(self):
        sketch = Sketch()
        sketch.update("é" * 256)
        with self.assertRaises(ValueError):
            sketch.update("é" * 257)

    def test_unicode_is_not_silently_normalized(self):
        sketch = Sketch()
        sketch.update("é", 10)
        sketch.update("e\u0301", 20)
        self.assertEqual(sketch.estimate("é"), 10)
        self.assertEqual(sketch.estimate("e\u0301"), 20)

    def test_overflow_update_is_atomic(self):
        sketch = Sketch()
        sketch.update("key", MAX_COUNT)
        before = sketch.dumps()
        with self.assertRaises(ValueError):
            sketch.update("other")
        self.assertEqual(sketch.dumps(), before)
        self.assertEqual(sketch.estimate("key"), MAX_COUNT)

    def test_invalid_configurations(self):
        for kwargs in (
            dict(width=31),
            dict(width=33),
            dict(width=True),
            dict(width=16384),
            dict(depth=0),
            dict(depth=9),
            dict(precision=6),
            dict(precision=15),
            dict(seed=-1),
            dict(seed=True),
            dict(seed=2**64),
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                Sketch(**kwargs)

    def test_configuration_is_read_only(self):
        sketch = Sketch()
        with self.assertRaises(AttributeError):
            sketch.width = 64
        config = sketch.config
        config["width"] = 64
        self.assertEqual(sketch.width, 2048)

    def test_collisions_can_overestimate_absent_keys(self):
        sketch = Sketch(width=32, depth=1)
        buckets = {}
        for i in range(1000):
            key = str(i)
            position = sketch._positions(key.encode())[0]
            if position in buckets:
                sketch.update(buckets[position], 9)
                self.assertEqual(sketch.estimate(key), 9)
                break
            buckets[position] = key
        else:
            self.fail("pigeonhole collision was not found")

    def test_hll_registers_against_independent_bit_string_oracle(self):
        sketch = Sketch(precision=7, seed=123)
        expected = [0] * 128
        for i in range(500):
            key = f"key-{i}"
            bits = "".join(
                f"{byte:08b}"
                for byte in hashlib.sha256(
                    b"hll1" + (123).to_bytes(8, "big") + key.encode()
                ).digest()[:8]
            )
            index = int(bits[:7], 2)
            suffix = bits[7:]
            rank = len(suffix) - len(suffix.lstrip("0")) + 1
            expected[index] = max(expected[index], rank)
            sketch.update(key)
        self.assertEqual(list(sketch._registers), expected)

    def test_100_seeded_streams_never_underestimate_and_merge_exactly(self):
        rng = random.Random(20261026)
        for case in range(100):
            whole = Sketch(width=64, depth=3, precision=7, seed=case)
            shards = [Sketch(**whole.config) for _ in range(3)]
            exact = Counter()
            for _ in range(100):
                key, weight = str(rng.randrange(70)), rng.randrange(1, 9)
                whole.update(key, weight)
                shards[rng.randrange(3)].update(key, weight)
                exact[key] += weight
            merged = shards[0].merge(shards[1]).merge(shards[2])
            with self.subTest(case=case):
                self.assertEqual(whole.dumps(), merged.dumps())
                self.assertTrue(all(whole.estimate(key) >= value for key, value in exact.items()))
                self.assertEqual(whole.total, sum(exact.values()))


class MergeTests(unittest.TestCase):
    def three(self):
        shards = [Sketch(width=64, precision=7) for _ in range(3)]
        for i, sketch in enumerate(shards):
            sketch.update("shared", i + 1)
            sketch.update(str(i), 5)
        return shards

    def test_associative_commutative_all_orders(self):
        a, b, c = self.three()
        expected = a.merge(b).merge(c).dumps()
        for x, y, z in itertools.permutations([a, b, c]):
            self.assertEqual(x.merge(y.merge(z)).dumps(), expected)

    def test_inputs_not_mutated_or_aliased(self):
        a, b, _ = self.three()
        before_a, before_b = a.dumps(), b.dumps()
        combined = a.merge(b)
        combined.update("new", 100)
        self.assertEqual(a.dumps(), before_a)
        self.assertEqual(b.dumps(), before_b)

    def test_duplicate_merge_doubles_mass_not_distinct_set(self):
        a, _, _ = self.three()
        merged = a.merge(a)
        self.assertEqual(merged.total, 2 * a.total)
        self.assertEqual(merged.estimate("shared"), 2 * a.estimate("shared"))
        self.assertEqual(merged.distinct(), a.distinct())

    def test_incompatible_dimensions_or_seed_rejected(self):
        base = Sketch()
        for kwargs in (dict(width=64), dict(depth=2), dict(precision=7), dict(seed=1)):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                base.merge(Sketch(**kwargs))

    def test_merge_overflow_rejected_without_mutation(self):
        a, b = Sketch(), Sketch()
        a.update("a", MAX_COUNT)
        b.update("b")
        before = a.dumps()
        with self.assertRaises(ValueError):
            a.merge(b)
        self.assertEqual(a.dumps(), before)

    def test_empty_is_merge_identity(self):
        a, _, _ = self.three()
        self.assertEqual(a.merge(Sketch(**a.config)).dumps(), a.dumps())


class FormatTests(unittest.TestCase):
    def state(self):
        sketch = Sketch(width=32, depth=2, precision=7, seed=19)
        sketch.update("one", 7)
        return sketch

    def test_roundtrip_and_continuing_updates(self):
        original = self.state()
        loaded = Sketch.loads(original.dumps())
        self.assertEqual(loaded.dumps(), original.dumps())
        original.update("two", 9)
        loaded.update("two", 9)
        self.assertEqual(original.dumps(), loaded.dumps())

    def test_size_is_exact_and_constant_as_labels_grow(self):
        sketch = Sketch()
        self.assertEqual(len(sketch.dumps()), 66620)
        for i in range(100):
            sketch.update(str(i))
        self.assertEqual(sketch.summary()["serialized_bytes"], len(sketch.dumps()))
        self.assertEqual(len(sketch.dumps()), 66620)

    def test_explicit_big_endian_header_and_counters(self):
        sketch = self.state()
        data = sketch.dumps()
        self.assertEqual(HEADER.unpack_from(data), (MAGIC, 32, 2, 7, 19, 7))
        values = [
            row[0] for row in struct.iter_unpack(">Q", data[HEADER.size : HEADER.size + 32 * 2 * 8])
        ]
        self.assertEqual(sum(values), 14)
        self.assertEqual(set(values), {0, 7})

    def test_bit_flip_checksum_and_truncation(self):
        data = self.state().dumps()
        for index in (0, HEADER.size + 20, len(data) - 1):
            corrupt = bytearray(data)
            corrupt[index] ^= 1
            with self.subTest(index=index), self.assertRaises(ValueError):
                Sketch.loads(bytes(corrupt))
        for value in (data[:-1], b"", b"x" * (MAX_STATE_BYTES + 1)):
            with self.assertRaises(ValueError):
                Sketch.loads(value)

    def test_forged_unsupported_header_rejected(self):
        data = self.state().dumps()
        for magic, width, depth, p, total in (
            (b"INVALID!", 32, 2, 7, 7),
            (MAGIC, 33, 2, 7, 7),
            (MAGIC, 32, 0, 7, 7),
            (MAGIC, 32, 2, 6, 7),
            (MAGIC, 32, 2, 7, MAX_COUNT + 1),
        ):
            body = HEADER.pack(magic, width, depth, p, 19, total) + data[HEADER.size : -32]
            with self.assertRaises(ValueError):
                Sketch.loads(rechecksum(body))

    def test_valid_checksum_does_not_hide_bad_row_sum(self):
        body = bytearray(self.state().dumps()[:-32])
        body[HEADER.size : HEADER.size + 8] = struct.pack(">Q", 12345)
        with self.assertRaisesRegex(ValueError, "row sums"):
            Sketch.loads(rechecksum(body))

    def test_invalid_hll_register_and_zero_mass_consistency(self):
        original = Sketch(width=32, depth=1, precision=7).dumps()
        for value in (1, 255):
            body = bytearray(original[:-32])
            body[HEADER.size + 32 * 8] = value
            with self.assertRaises(ValueError):
                Sketch.loads(rechecksum(body))

    def test_saturated_registers_are_not_reported_as_reliable_estimate(self):
        sketch = Sketch(width=32, depth=1, precision=7)
        for i in range(128):
            sketch.update(str(i))
        body = bytearray(sketch.dumps()[:-32])
        body[HEADER.size + 32 * 8 :] = bytes([58]) * 128
        loaded = Sketch.loads(rechecksum(body))
        with self.assertRaisesRegex(ValueError, "unsaturated range"):
            loaded.distinct()


class IoTests(unittest.TestCase):
    def test_jsonl_weighted_fixture(self):
        sketch = build(ROOT / "examples/worker-a.jsonl")
        self.assertEqual(sketch.total, 19)
        self.assertEqual(sketch.estimate("/checkout"), 12)

    def test_invalid_jsonl_shapes_and_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            for contents in (
                "\n",
                "{}\n",
                '{"key":"a","key":"b","count":1}\n',
                '{"key":"a","count":0}\n',
                '{"key":"a","count":true}\n',
                '{"key":"a","count":NaN}\n',
                '{"key":"a","count":1,"extra":1}\n',
            ):
                path.write_text(contents, encoding="utf-8")
                with self.subTest(contents=contents), self.assertRaises(ValueError):
                    build(path)

    def test_input_line_and_utf8_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            for contents in (b" " * 4097, b"\xff\n"):
                path.write_bytes(contents)
                with self.assertRaises(ValueError):
                    build(path)

    def test_binary_file_refuses_overwrite_and_roundtrips(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.skr"
            sketch = Sketch()
            sketch.update("one")
            save(sketch, path)
            self.assertEqual(load(path).dumps(), sketch.dumps())
            before = path.read_bytes()
            with self.assertRaises(FileExistsError):
                save(Sketch(), path)
            self.assertEqual(path.read_bytes(), before)


class CliTests(unittest.TestCase):
    def cli(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "sketchrelay", *map(str, args)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=15,
        )

    def test_build_merge_query_and_inspect(self):
        with tempfile.TemporaryDirectory() as directory:
            a, b, merged = [Path(directory) / name for name in ("a.skr", "b.skr", "merged.skr")]
            for name, path in (("worker-a", a), ("worker-b", b)):
                result = self.cli("build", ROOT / "examples" / f"{name}.jsonl", "--output", path)
                self.assertEqual(result.returncode, 0, result.stderr)
            result = self.cli("merge", a, b, "--output", merged)
            self.assertEqual(result.returncode, 0, result.stderr)
            result = self.cli("query", merged, "/checkout")
            self.assertEqual(json.loads(result.stdout)["frequency_estimates"]["/checkout"], 20)
            result = self.cli("inspect", merged)
            self.assertEqual(json.loads(result.stdout)["total_mass"], 34)

    def test_invalid_stream_creates_no_snapshot_or_stdout(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / "bad.jsonl", Path(directory) / "output.skr"
            source.write_text('{"key":"good","count":2}\ninvalid\n', encoding="utf-8")
            result = self.cli("build", source, "--output", output)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, "")
            self.assertFalse(output.exists())

    def test_existing_output_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "precious.skr"
            path.write_bytes(b"preserve me")
            result = self.cli("build", ROOT / "examples/worker-a.jsonl", "--output", path)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(path.read_bytes(), b"preserve me")

    def test_incompatible_merge_creates_no_output(self):
        with tempfile.TemporaryDirectory() as directory:
            a, b, out = [Path(directory) / name for name in ("a.skr", "b.skr", "out.skr")]
            save(Sketch(seed=0), a)
            save(Sketch(seed=1), b)
            result = self.cli("merge", a, b, "--output", out)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, "")
            self.assertFalse(out.exists())


if __name__ == "__main__":
    unittest.main()
