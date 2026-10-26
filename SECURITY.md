# Trust and safety boundaries

SketchRelay processes local JSONL and bounded binary snapshots. It does not
unpickle state, execute label values, invoke a network service, or send telemetry
elsewhere. Output is written only to a new path using exclusive creation.

All input counts are positive and total mass is bounded. Invalid updates and
overflow leave a live sketch unchanged. The CLI fully validates/builds before
writing a new snapshot, but a write/flush failure may leave a partial new file.
There is no crash-safe publication, transaction log, automatic cleanup, or
overwrite command. Keep source telemetry if exact reconstruction is required.

Binary SHA-256 checksums detect accidental corruption. They do not authenticate
the sender. Even a structurally valid, correctly checksummed snapshot can be
forged to misrepresent events. Use an authenticated transport and trusted
delivery identities outside this library if combining summaries from workers.

The seed is a public configuration value, not a secret. Hash-based summaries
are not differential privacy, anonymization, or encryption. Known-label queries
can reveal information; do not publish summaries of sensitive events merely
because the original strings are absent from the serialized state.

Merge does not detect duplicate or overlapping delivery. Frequency mass is
additive even when a snapshot is repeated, while HLL merges are set-idempotent.
No sketch estimate should authorize access, determine an exact bill, or replace
an exact safety-critical counter. Adversarially chosen labels and hash-collision
behavior are outside any claimed accuracy guarantee.

Byte/count/dimension limits are not hard memory or CPU quotas. Use OS-level
resource limits for untrusted workloads. Prefer GitHub private vulnerability
reporting where available, and never include secrets in public issues.
