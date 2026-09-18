#!/usr/bin/env python3
"""Prove S3 writes and query continuity over a RustFS restart in smoke's project."""
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from checkpoint import Stack
from smoke_assertions import ingest_marker, verify_marker


def check(env_file, origin):
    stack = Stack(env_file)
    if stack.mode != 's3':
        raise RuntimeError('S3 smoke requires the s3 profile')
    proof = ingest_marker(env_file, origin, stack.state)
    stack.dc('exec', '-T', 'caddy', 'wget', '-qO-', '--post-data=', 'http://loki:3100/flush')
    stack.dc('exec', '-T', 'caddy', 'wget', '-qO-', '-T', '180', '--post-data=',
             'http://mimir:8080/ingester/flush?wait=true')

    def keys(bucket):
        output = stack.dc('run', '--rm', '--no-deps', '-T', '--entrypoint', '/bin/sh', 'rustfs-init', '-ec',
                          'curl --aws-sigv4 aws:amz:us-east-1:s3 '
                          '-u "$OB_S3_ACCESS_KEY:$OB_S3_SECRET_KEY" -fsS '
                          '"http://rustfs:9000/$1?list-type=2"', 'sh', bucket)
        document = ET.fromstring(output)
        if document.findtext('{*}IsTruncated') == 'true':
            raise RuntimeError('unexpectedly large disposable S3 dataset')
        return {item.text for item in document.findall('.//{*}Key')}

    deadline = time.monotonic() + 180
    while True:
        before = {bucket: keys(bucket) for bucket in ('loki', 'mimir-blocks')}
        if all(before.values()):
            break
        if time.monotonic() >= deadline:
            raise RuntimeError('Loki and Mimir did not persist objects to RustFS')
        time.sleep(3)
    stack.dc('restart', 'rustfs')
    stack.dc('up', '-d', '--wait', '--wait-timeout', '300')
    for bucket, expected in before.items():
        if not expected <= keys(bucket):
            raise RuntimeError('RustFS objects disappeared after restart: ' + bucket)
    verify_marker(env_file, origin, proof)
    print('S3 Smoke Contract: PASS (objects preserved; original log and metric queryable after RustFS restart)', flush=True)


if __name__ == '__main__':
    check(Path(sys.argv[1]), sys.argv[2])
