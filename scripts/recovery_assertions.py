#!/usr/bin/env python3
"""Historical recovery proofs, independent of host Docker log discovery."""
import hashlib
import json
import secrets
import subprocess
import tempfile
import time
import urllib.parse
import xml.etree.ElementTree as ET

from smoke_assertions import client, verify_marker


def ingest(stack, origin):
    _, api, eventually = client(stack.env_file, origin)
    marker = secrets.token_hex(12)
    timestamp = time.time_ns()
    payload = {'streams': [{'stream': {'job': 'recovery-drill'},
                           'values': [[str(timestamp), 'checkpoint-marker-' + marker]]}]}
    stack.dc('exec', '-T', 'caddy', 'wget', '-qO-', '--header=Content-Type: application/json',
             '--post-data=' + json.dumps(payload), 'http://loki:3100/loki/api/v1/push')
    metric = 'stack_persistence_marker{marker="' + marker + '"}'
    textfile = stack.state / 'textfile/persistence-marker.prom'
    try:
        textfile.write_text(metric + ' 42\n')
        textfile.chmod(0o644)
        result = eventually('/api/datasources/proxy/uid/mimir/api/v1/query?' +
                            urllib.parse.urlencode({'query': metric}),
                            lambda data: any(float(row['value'][1]) == 42 for row in data['data']['result']))
        query_time = result['data']['result'][0]['value'][0]
    finally:
        textfile.unlink(missing_ok=True)
    trace = ingest_trace(stack, marker, timestamp)
    dashboard = {'uid': 'recovery-' + marker, 'title': 'Recovery ' + marker,
                 'tags': ['checkpoint-' + marker], 'schemaVersion': 39, 'panels': []}
    api('/api/dashboards/db', json.dumps({'dashboard': dashboard}).encode())
    proof = {'marker': marker, 'timestamp': timestamp, 'query_time': query_time,
             'log_job': 'recovery-drill', **trace,
             'dashboard': dashboard}
    verify(stack.env_file, origin, proof)
    print('ok: historical log, metric, trace and operator dashboard seeded; producers removed', flush=True)
    return proof


def ingest_trace(stack, marker, timestamp):
    trace_id = secrets.token_hex(16)
    span_name = 'checkpoint-span-' + marker
    payload = {'resourceSpans': [{'scopeSpans': [{'spans': [{
        'traceId': trace_id, 'spanId': secrets.token_hex(8), 'name': span_name, 'kind': 1,
        'startTimeUnixNano': str(timestamp), 'endTimeUnixNano': str(timestamp + 1000000),
    }]}]}]}
    stack.dc('exec', '-T', 'caddy', 'wget', '-qO-', '--header=Content-Type: application/json',
             '--post-data=' + json.dumps(payload), 'http://ob-alloy-otlp:4318/v1/traces')
    return {'trace_id': trace_id, 'span_name': span_name}


def verify(env_file, origin, proof):
    verify_marker(env_file, origin, proof)
    verify_trace(env_file, origin, proof)
    _, api, _ = client(env_file, origin)
    saved = api('/api/dashboards/uid/' + proof['dashboard']['uid'])['dashboard']
    for key in ('uid', 'title', 'tags', 'panels'):
        if saved[key] != proof['dashboard'][key]:
            raise RuntimeError('operator-created Grafana dashboard was not recovered')


def verify_trace(env_file, origin, proof):
    _, _, eventually = client(env_file, origin)
    def has_span(value):
        if isinstance(value, dict):
            return value.get('name') == proof['span_name'] or any(has_span(item) for item in value.values())
        return isinstance(value, list) and any(has_span(item) for item in value)
    eventually('/api/datasources/proxy/uid/tempo/api/traces/' + proof['trace_id'], has_span)


def s3_inventory(stack, buckets=('loki', 'mimir-blocks')):
    """Hash actual object bodies, so intact metadata cannot hide corrupt extraction."""
    result = {}
    for bucket in buckets:
        output = stack.dc('run', '--rm', '--no-deps', '-T', '--entrypoint', '/bin/sh', 'rustfs-init', '-ec',
                          'curl --aws-sigv4 aws:amz:us-east-1:s3 '
                          '-u "$OB_S3_ACCESS_KEY:$OB_S3_SECRET_KEY" -fsS '
                          '"http://rustfs:9000/$1?list-type=2"', 'sh', bucket)
        document = ET.fromstring(output)
        if document.findtext('{*}IsTruncated') == 'true':
            raise RuntimeError('unexpectedly large disposable S3 dataset')
        entries = {}
        for item in document.findall('{*}Contents'):
            key = item.findtext('{*}Key')
            if not key:
                raise RuntimeError('S3 inventory contains an empty object key')
            # rustfs-init has a read-only root filesystem; hash streamed bytes on the host.
            command = stack.command + [
                'run', '--rm', '--no-deps', '-T', '--entrypoint', '/bin/sh', 'rustfs-init', '-ec',
                'curl --aws-sigv4 aws:amz:us-east-1:s3 '
                '-u "$OB_S3_ACCESS_KEY:$OB_S3_SECRET_KEY" -fsS --max-time 60 "$1"', 'sh',
                'http://rustfs:9000/' + bucket + '/' + urllib.parse.quote(key, safe='/')]
            with tempfile.TemporaryFile() as body:
                response = subprocess.run(command, stdout=body, stderr=subprocess.PIPE)
                if response.returncode:
                    raise RuntimeError('could not read disposable S3 object body')
                body.seek(0)
                digest = hashlib.sha256()
                for block in iter(lambda: body.read(1024 * 1024), b''):
                    digest.update(block)
                entries[key] = digest.hexdigest()
        result[bucket] = entries
    return result


def flush_s3(stack, buckets=('loki', 'mimir-blocks')):
    stack.dc('exec', '-T', 'caddy', 'wget', '-qO-', '--post-data=', 'http://loki:3100/flush')
    stack.dc('exec', '-T', 'caddy', 'wget', '-qO-', '-T', '180', '--post-data=',
             'http://mimir:8080/ingester/flush?wait=true')
    deadline = time.monotonic() + 180
    while True:
        objects = s3_inventory(stack, buckets=buckets)
        if all(objects.values()):
            return objects
        if time.monotonic() >= deadline:
            raise RuntimeError('backends did not persist objects: ' + ', '.join(buckets))
        time.sleep(3)


def verify_objects(before, after):
    for bucket, entries in before.items():
        if not entries or any(after.get(bucket, {}).get(key) != digest for key, digest in entries.items()):
            raise RuntimeError('restored S3 object bodies differ: ' + bucket)
