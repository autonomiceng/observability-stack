#!/usr/bin/env python3
"""HTTP assertions for smoke.sh; uses Grafana's authenticated datasource proxy."""

import base64
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from bootstrap import read_env


def client(env_file, origin):
    _, secrets = read_env(env_file)
    auth = base64.b64encode(('admin:' + secrets['OB_GRAFANA_ADMIN_PASSWORD']).encode()).decode()
    grafana = 'http://grafana.' + origin

    def get(url, authenticated=False, data=None):
        # glibc does not resolve *.localhost; connect to loopback and send the hostname.
        split = urllib.parse.urlsplit(url)
        headers = {'Host': split.netloc, 'Content-Type': 'application/json'}
        if authenticated:
            headers['Authorization'] = 'Basic ' + auth
        port = split.port or 80
        target = f"{split.scheme}://127.0.0.1:{port}{split.path}{'?' + split.query if split.query else ''}"
        with urllib.request.urlopen(urllib.request.Request(target, headers=headers, data=data), timeout=10) as response:
            return response.read().decode()

    def api(path, data=None):
        body = get(grafana + path, True, data)
        return json.loads(body) if body else None

    def eventually(path, predicate):
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            try:
                result = api(path)
                if predicate(result):
                    return result
            except (urllib.error.URLError, ValueError):
                pass
            time.sleep(3)
        raise AssertionError('no matching data before deadline: ' + path)

    return get, api, eventually


def check(env_file: Path, origin: str, project: str) -> None:
    get, api, eventually = client(env_file, origin)
    base = 'http://' + origin
    grafana = 'http://grafana.' + origin

    for service in ('grafana', 'loki', 'tempo', 'mimir', 'alloy'):
        get(base + '/health/' + service)
    print('ok: all five HTTP readiness endpoints', flush=True)
    assert 'Observability Stack' in get(base + '/')
    assert 'grafana' in json.loads(get(base + '/versions.json'))['images']
    print('ok: console and versions', flush=True)

    datasources = api('/api/datasources')
    assert {(ds['uid'], ds['type']) for ds in datasources} == {('mimir', 'prometheus'), ('loki', 'loki'), ('tempo', 'tempo')}
    dashboard = api('/api/dashboards/uid/stacks-overview')
    assert dashboard['meta']['folderTitle'] == 'Stacks'
    assert len(dashboard['dashboard']['panels']) == 4
    assert {rule['uid'] for rule in api('/api/v1/provisioning/alert-rules')} == {'valkey-memory', 'backplane-archive-age', 'checkpoint-age', 'scrape-target-down', 'host-disk-bytes', 'host-disk-inodes', 'loki-ingestion-failures', 'mimir-ingestion-failures', 'tempo-ingestion-failures', 'alloy-remote-write-backlog', 'gateway-checkpoint-age', 'gateway-checkpoint-failed', 'gateway-archiver-failures'}
    rule = api('/api/v1/provisioning/alert-rules/scrape-target-down')
    assert '== bool 0' in next(item['model']['expr'] for item in rule['data'] if item['refId'] == 'A')
    try:
        get(grafana + '/api/datasources')
    except urllib.error.HTTPError as error:
        assert error.code == 401, error.code
    else:
        raise AssertionError('anonymous datasource access enabled')
    print('ok: generated admin password, anonymous disabled, datasources/dashboard/alerts provisioned', flush=True)

    query = urllib.parse.urlencode({'query': 'up{job="alloy"}'})
    eventually('/api/datasources/proxy/uid/mimir/api/v1/query?' + query,
               lambda data: data.get('status') == 'success' and any(float(row['value'][1]) == 1 for row in data['data']['result']))
    print('ok: Mimir contains successful Alloy self-scrape', flush=True)

    import bootstrap
    lines, _ = bootstrap.read_env(env_file)
    settings = {m['key']: bootstrap.unquote(m['value']) for m in map(bootstrap.ENV_LINE.match, lines) if m}
    ingest_marker(env_file, origin, Path(settings['OB_STATE_DIR']))
    query = urllib.parse.urlencode({'query': '{compose_project="' + project + '"}',
                                   'start': str(time.time_ns() - 600 * 10**9)})
    assert not api('/api/datasources/proxy/uid/loki/loki/api/v1/query_range?' + query)['data']['result']
    print('ok: disposable project logs excluded; independent producer collected', flush=True)
    query = urllib.parse.urlencode({'query': 'up{job=~"loki|mimir|tempo"}'})
    eventually('/api/datasources/proxy/uid/mimir/api/v1/query?' + query,
               lambda data: len(data['data']['result']) == 3 and
               all(float(row['value'][1]) == 1 for row in data['data']['result']))
    query = urllib.parse.urlencode({'query': 'node_filesystem_avail_bytes{job="checkpoints",fstype!="tmpfs"}'})
    eventually('/api/datasources/proxy/uid/mimir/api/v1/query?' + query,
               lambda data: any(float(row['value'][1]) > 0 for row in data['data']['result']))
    print('ok: all three Backend scrapes and host filesystem metrics', flush=True)
    print('Smoke Contract: PASS', flush=True)


def ingest_marker(env_file, origin, state):
    import secrets
    import subprocess
    _, api, eventually = client(env_file, origin)
    marker = secrets.token_hex(12)
    timestamp = time.time_ns()
    # An unlabelled producer exercises Docker collection while disposable Compose logs are dropped.
    root = Path(__file__).resolve().parent.parent
    config = subprocess.run(['docker', 'compose', '--project-directory', str(root),
                             '--env-file', str(env_file), 'config', '--format', 'json'],
                            check=True, capture_output=True, text=True)
    image = json.loads(config.stdout)['services']['caddy']['image']
    producer = subprocess.run(['docker', 'run', '-d', '--network', 'none', '--memory', '32m',
                              '--entrypoint', 'sh', image, '-c',
                              'while true; do echo "checkpoint-marker-' + marker + '"; sleep 1; done'],
                             check=True, capture_output=True, text=True).stdout.strip()
    metric = 'stack_persistence_marker{marker="' + marker + '"}'
    textfile = state / 'textfile' / 'persistence-marker.prom'
    ingested = False
    try:
        textfile.write_text(metric + ' 42\n')
        textfile.chmod(0o644)
        result = eventually('/api/datasources/proxy/uid/mimir/api/v1/query?' +
                            urllib.parse.urlencode({'query': metric}),
                            lambda data: any(float(row['value'][1]) == 42 for row in data['data']['result']))
        query_time = result['data']['result'][0]['value'][0]
        proof = {'marker': marker, 'timestamp': timestamp, 'query_time': query_time}
        verify_marker(env_file, origin, proof)
        ingested = True
    finally:
        cleanup_failed = False
        try:
            textfile.unlink(missing_ok=True)
        except OSError:
            cleanup_failed = True
        try:
            result = subprocess.run(['docker', 'rm', '-f', producer], check=False, capture_output=True)
            cleanup_failed = cleanup_failed or result.returncode != 0
        except OSError:
            cleanup_failed = True
        if cleanup_failed:
            print('marker cleanup failed; inspect disposable producer ' + producer, file=sys.stderr)
            if ingested:
                raise RuntimeError('marker cleanup failed')
    print('ok: unique marker log and metric ingested; producers removed', flush=True)
    return proof


def verify_marker(env_file, origin, proof):
    _, _, eventually = client(env_file, origin)
    query = urllib.parse.urlencode({
        'query': '{job="' + proof.get('log_job', 'docker') + '"} |= "checkpoint-marker-' + proof['marker'] + '"',
        'start': str(proof['timestamp'] - 60 * 10**9), 'end': str(proof['timestamp'] + 120 * 10**9),
    })
    eventually('/api/datasources/proxy/uid/loki/loki/api/v1/query_range?' + query,
               lambda data: any(value[1] == 'checkpoint-marker-' + proof['marker']
                                for row in data['data']['result'] for value in row['values']))
    query = urllib.parse.urlencode({'query': 'stack_persistence_marker{marker="' + proof['marker'] + '"}',
                                    'time': proof['query_time']})
    eventually('/api/datasources/proxy/uid/mimir/api/v1/query?' + query,
               lambda data: any(float(row['value'][1]) == 42 for row in data['data']['result']))


if __name__ == '__main__':
    check(Path(sys.argv[1]), sys.argv[2], sys.argv[3])
