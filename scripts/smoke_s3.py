#!/usr/bin/env python3
"""Prove S3 writes and query continuity over a RustFS restart in smoke's project."""
import sys
from pathlib import Path

import recovery_assertions
from checkpoint import Stack
from smoke_assertions import ingest_marker, verify_marker


def check(env_file, origin):
    stack = Stack(env_file)
    if stack.mode != 's3':
        raise RuntimeError('S3 smoke requires the s3 profile')
    proof = ingest_marker(env_file, origin, stack.state)
    trace = recovery_assertions.ingest_trace(stack, proof['marker'], proof['timestamp'])
    recovery_assertions.verify_trace(env_file, origin, trace)
    buckets = ('loki', 'mimir-blocks', 'tempo')
    # Tempo 3 monolithic live-store flushes on its own block timer; it has no /flush API.
    before = recovery_assertions.flush_s3(stack, buckets=buckets)
    stack.dc('restart', 'rustfs')
    stack.dc('up', '-d', '--wait', '--wait-timeout', '300')
    recovery_assertions.verify_objects(before, recovery_assertions.s3_inventory(stack, buckets=buckets))
    verify_marker(env_file, origin, proof)
    recovery_assertions.verify_trace(env_file, origin, trace)
    print('S3 Smoke Contract: PASS (object bodies preserved; original log, metric and trace queryable '
          'after RustFS restart)', flush=True)


if __name__ == '__main__':
    check(Path(sys.argv[1]), sys.argv[2])
