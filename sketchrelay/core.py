"""Mergeable Count-Min frequencies and 64-bit HyperLogLog cardinalities."""

from array import array
import hashlib
import hmac
import math
import struct

MAX_COUNT = 2**63 - 1
MAX_STATE_BYTES = 1_000_000
HEADER = struct.Struct(">8sHBBQQ")
MAGIC = b"SKRLY001"


def key_bytes(key):
    if type(key) is not str:
        raise ValueError("key must be a string")
    encoded = key.encode("utf-8")
    if not 1 <= len(encoded) <= 512:
        raise ValueError("key must contain 1-512 UTF-8 bytes")
    return encoded


class Sketch:
    def __init__(self, *, width=2048, depth=4, precision=10, seed=0):
        if type(width) is not int or not 32 <= width <= 8192 or width & (width - 1):
            raise ValueError("width must be a power of two from 32 through 8192")
        if type(depth) is not int or not 1 <= depth <= 8:
            raise ValueError("depth must be integer 1-8")
        if type(precision) is not int or not 7 <= precision <= 14:
            raise ValueError("HLL precision must be integer 7-14")
        if type(seed) is not int or not 0 <= seed < 2**64:
            raise ValueError("seed must be an unsigned 64-bit integer")
        self._width, self._depth, self._precision, self._seed = width, depth, precision, seed
        self._counts = array("Q", [0]) * (width * depth)
        self._registers = bytearray(1 << precision)
        self._total = 0

    @property
    def total(self):
        return self._total

    @property
    def width(self):
        return self._width

    @property
    def depth(self):
        return self._depth

    @property
    def precision(self):
        return self._precision

    @property
    def seed(self):
        return self._seed

    @property
    def config(self):
        return dict(width=self.width, depth=self.depth, precision=self.precision, seed=self.seed)

    def _positions(self, key):
        prefix = b"cms1" + self.seed.to_bytes(8, "big")
        return [
            row * self.width
            + (
                int.from_bytes(hashlib.sha256(prefix + bytes([row]) + key).digest()[:8], "big")
                & (self.width - 1)
            )
            for row in range(self.depth)
        ]

    def _register(self, key):
        hashed = int.from_bytes(
            hashlib.sha256(b"hll1" + self.seed.to_bytes(8, "big") + key).digest()[:8], "big"
        )
        remainder_bits = 64 - self.precision
        slot = hashed >> remainder_bits
        remainder = hashed & ((1 << remainder_bits) - 1)
        rank = remainder_bits - remainder.bit_length() + 1
        return slot, rank

    def update(self, key, count=1):
        encoded = key_bytes(key)
        if type(count) is not int or not 1 <= count <= MAX_COUNT - self.total:
            raise ValueError("positive integer count required; total must fit signed 64 bits")
        positions = self._positions(encoded)
        slot, rank = self._register(encoded)
        # Validate and hash before mutating: a rejected update leaves both sketches unchanged.
        for position in positions:
            self._counts[position] += count
        self._registers[slot] = max(self._registers[slot], rank)
        self._total += count

    def estimate(self, key):
        return min(self._counts[position] for position in self._positions(key_bytes(key)))

    def distinct(self):
        m = len(self._registers)
        raw = (
            (0.7213 / (1 + 1.079 / m))
            * m
            * m
            / math.fsum(2.0 ** (-value) for value in self._registers)
        )
        empty = self._registers.count(0)
        if empty and raw <= 2.5 * m:
            return m * math.log(m / empty)
        # No large-range bias correction is claimed near 64-bit hash saturation.
        if raw >= 2**64 / 30:
            raise ValueError("cardinality estimate is outside the supported unsaturated range")
        return raw

    def merge(self, other):
        if not isinstance(other, Sketch) or self.config != other.config:
            raise ValueError("only identical width/depth/precision/seed configurations can merge")
        if self.total + other.total > MAX_COUNT:
            raise ValueError("merged total overflows signed 64 bits")
        combined = Sketch(**self.config)
        combined._counts = array("Q", (a + b for a, b in zip(self._counts, other._counts)))
        combined._registers = bytearray(
            max(a, b) for a, b in zip(self._registers, other._registers)
        )
        combined._total = self.total + other.total
        return combined

    def dumps(self):
        # Explicit big-endian serialization is independent of array's native byte order.
        body = HEADER.pack(MAGIC, self.width, self.depth, self.precision, self.seed, self.total)
        body += b"".join(struct.pack(">Q", value) for value in self._counts)
        body += bytes(self._registers)
        return body + hashlib.sha256(body).digest()

    @classmethod
    def loads(cls, data):
        if type(data) is not bytes or not HEADER.size + 32 <= len(data) <= MAX_STATE_BYTES:
            raise ValueError("invalid sketch byte length")
        body, checksum = data[:-32], data[-32:]
        if not hmac.compare_digest(hashlib.sha256(body).digest(), checksum):
            raise ValueError("sketch checksum mismatch")
        magic, width, depth, precision, seed, total = HEADER.unpack_from(body)
        if magic != MAGIC or total > MAX_COUNT:
            raise ValueError("unsupported format or invalid total")
        result = cls(width=width, depth=depth, precision=precision, seed=seed)
        expected = HEADER.size + width * depth * 8 + (1 << precision)
        if len(body) != expected:
            raise ValueError("sketch dimensions do not match byte length")
        end = HEADER.size + width * depth * 8
        result._counts = array(
            "Q", (row[0] for row in struct.iter_unpack(">Q", body[HEADER.size : end]))
        )
        result._registers = bytearray(body[end:])
        result._total = total
        if any(
            sum(result._counts[row * width : (row + 1) * width]) != total for row in range(depth)
        ):
            raise ValueError("frequency row sums do not match total")
        if any(value > 65 - precision for value in result._registers):
            raise ValueError("invalid HLL register")
        occupied = sum(value != 0 for value in result._registers)
        if occupied > total or (total > 0 and occupied == 0):
            raise ValueError("cardinality registers disagree with total")
        return result

    def summary(self):
        return dict(
            config=self.config,
            total_mass=self.total,
            distinct_estimate=self.distinct(),
            serialized_bytes=HEADER.size + self.width * self.depth * 8 + (1 << self.precision) + 32,
            frequency_cells=self.width * self.depth,
            hll_registers=1 << self.precision,
        )
