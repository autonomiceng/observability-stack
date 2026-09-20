#!/usr/bin/env python3
"""HTTP assertions for smoke.sh; uses Grafana's authenticated datasource proxy."""

import base64
import http.client
import json
import re
import secrets
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import bootstrap
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
    lines, _ = read_env(env_file)
    settings = {match['key']: bootstrap.unquote(match['value']) for match in map(bootstrap.ENV_LINE.match, lines) if match}
    check_access(env_file, settings)
    check_rustfs_console(settings)
    from smoke_status import check as check_status
    check_status(env_file, settings)

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


def check_proxy_access(env_file, settings):
    authority = settings['OB_GRAFANA_AUTHORITY']
    host = settings['OB_GRAFANA_URL_HOST']
    port = int(settings['OB_HTTP_PORT'])

    def request(authority, path, authenticated=False):
        connection = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
        try:
            headers = {'Host': authority}
            if authenticated:
                _, saved = read_env(env_file)
                headers['Authorization'] = 'Basic ' + base64.b64encode(
                    ('admin:' + saved['OB_GRAFANA_ADMIN_PASSWORD']).encode()).decode()
            connection.request('GET', path, headers=headers)
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    for authority_value in (authority, settings['OB_GRAFANA_HOST']):
        assert request(authority_value, '/login')[0] == 200
        assert request(authority_value, '/api/datasources')[0] == 401
        for path in ('/metrics', '/metrics/', '/METRICS'):
            assert request(authority_value, path)[0] == 404
    status, body = request(authority, '/api/frontend/settings', authenticated=True)
    assert status == 200
    frontend = json.loads(body)
    assert frontend['appUrl'] == settings['OB_GRAFANA_URL'] + '/'
    # The same machine's other ports and bare hostname stay on console routes.
    for other in (host, host + ':8443', host + ':8445', host + ':443', settings['OB_PUBLIC_DOMAIN']):
        status, body = request(other, '/')
        assert status == 200 and b'Observability Stack' in body, other
        assert request(other, '/login')[0] == 404, other
        assert request(other, '/health/grafana')[0] == 200, other
        status, body = request(other, '/links.json')
        assert status == 200
        expected_links = {'grafana': settings['OB_GRAFANA_URL'],
                          'gateway': settings['OB_GATEWAY_URL'], 'backplane': settings['OB_BACKPLANE_URL']}
        if settings['OB_RUSTFS_CONSOLE'] == 'true':
            expected_links['rustfs'] = bootstrap.rustfs_origin(settings)
        assert json.loads(body) == expected_links
    print('ok: exact external authority, internal Grafana, same-host sibling ports, root health, links, auth and metrics denial', flush=True)


def check_rustfs_console(settings):
    origin = bootstrap.rustfs_origin(settings)
    authority = urllib.parse.urlsplit(origin).netloc

    def request(path, method='GET', headers=None, host=authority):
        connection = http.client.HTTPConnection('127.0.0.1', int(settings['OB_HTTP_PORT']), timeout=10)
        try:
            connection.request(method, path, headers={'Host': host} | (headers or {}))
            response = connection.getresponse()
            return response.status, response.getheader('Location'), response.read()
        finally:
            connection.close()

    if settings['OB_RUSTFS_CONSOLE'] != 'true':
        for path in ('/', '/rustfs/console/', '/rustfs/admin/v3/accountinfo', '/?Action=AssumeRole'):
            assert request(path)[0] == 404, path
        print('ok: disabled RustFS origin returns 404', flush=True)
        return
    for method in ('GET', 'HEAD'):
        status, location, _ = request('/', method, {'Accept': 'text/html'})
        assert (status, location) == (302, '/rustfs/console/')
    for method, headers in (('GET', {}), ('POST', {'Accept': 'text/html'})):
        status, location, _ = request('/', method, headers)
        assert status >= 400 and location is None, (method, status)
    status, _, html = request('/rustfs/console/')
    assert status == 200 and b'<html' in html.lower()
    assets = set(re.findall(r"[\"']([^\"'<>\s]+\.(?:js|css)(?:\?[^\"'<>\s]*)?)[\"']", html.decode()))
    assert assets, 'console must reference assets'
    for asset in assets:
        url = urllib.parse.urlsplit(urllib.parse.urljoin(origin + '/rustfs/console/', asset))
        assert url.netloc == authority, 'console asset must retain its origin'
        status, location, body = request(url.path + ('?' + url.query if url.query else ''))
        assert status == 200 and location is None and body and b'<html' not in body[:100].lower(), url.path
    assert request('/rustfs/admin/v3/accountinfo')[0] == 403
    # A direct, untrusted caller cannot replace its allowed peer identity or route via forwarding headers.
    assert request('/rustfs/console/', headers={'X-Forwarded-For': '203.0.113.200',
                   'X-Forwarded-Host': 'spoof.invalid:8451', 'X-Forwarded-Proto': 'https'})[0] == 200
    if settings['OB_ACCESS_MODE'] == 'proxy':
        wrong_authority = urllib.parse.urlsplit(origin).hostname + ':8452'
        assert request('/rustfs/admin/v3/accountinfo', host=wrong_authority)[0] == 404
        assert request('/rustfs/console/', host=wrong_authority,
                       headers={'X-Forwarded-Host': authority})[0] == 404
    print(f'ok: RustFS whole origin, {len(assets)} assets, HTML-only landing, unsigned admin denial and spoof isolation', flush=True)


def check_access(env_file, settings):
    if settings['OB_ACCESS_MODE'] == 'proxy':
        check_proxy_access(env_file, settings)
        return
    command = ['docker', 'compose', '--env-file', str(env_file)]
    certificate = subprocess.run(command + ['exec', '-T', 'caddy', 'cat',
                                 '/data/caddy/pki/authorities/local/root.crt'],
                                 check=True, capture_output=True, text=True).stdout
    context = ssl.create_default_context(cadata=certificate)
    for scheme in ('http', 'https'):
        for host in ('localhost', '127.0.0.1', 'grafana.localhost'):
            if scheme == 'https':
                connection = bootstrap.LocalHTTPSConnection(host, port=int(settings['OB_HTTPS_PORT']), timeout=10, context=context)
            else:
                connection = http.client.HTTPConnection('127.0.0.1', int(settings['OB_HTTP_PORT']), timeout=10)
            try:
                connection.request('GET', '/login' if host.startswith('grafana.') else '/', headers={'Host': host})
                response = connection.getresponse()
                assert response.status == 200, (scheme, host, response.status)
                assert response.getheader('Location') is None
                assert response.getheader('Strict-Transport-Security') is None
                response.read()
            finally:
                connection.close()
    print('ok: local HTTP and verified HTTPS, IP root and explicit Grafana, no redirect/HSTS', flush=True)
    marker = secrets.token_hex(12)
    credential = secrets.token_hex(24)
    connection = http.client.HTTPConnection('127.0.0.1', int(settings['OB_HTTP_PORT']), timeout=10)
    try:
        connection.request('GET', '/?token=' + credential, headers={
            'Host': 'alias-' + marker + '.invalid', 'Authorization': 'Bearer ' + credential,
            'Cookie': 'session=' + credential, 'X-Api-Key': credential})
        response = connection.getresponse()
        assert response.status == 200
        response.read()
        connection.request('GET', '/links.json', headers={'Host': 'alias-' + marker + '.invalid'})
        response = connection.getresponse()
        assert json.loads(response.read())['grafana'] == bootstrap.grafana_origin(settings)
    finally:
        connection.close()
    container = subprocess.run(command + ['ps', '-q', 'caddy'], check=True, capture_output=True, text=True).stdout.strip()
    logs = subprocess.run(['docker', 'logs', container], check=True, capture_output=True, text=True)
    assert credential not in logs.stdout + logs.stderr
    entries = [json.loads(line) for line in logs.stdout.splitlines() if line.startswith('{')]
    assert any(entry.get('request', {}).get('host') == 'alias-' + marker + '.invalid' and
               entry['request']['uri'] == '/' for entry in entries)
    assert all('http.log.access' not in json.loads(line).get('logger', '')
               for line in logs.stderr.splitlines() if line.startswith('{'))
    assert any(line.startswith('{') for line in logs.stderr.splitlines())
    print('ok: JSON access stdout, runtime stderr, header/query redaction and configured alias links', flush=True)


def ingest_marker(env_file, origin, state):
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
                              '--log-driver=journald', '--log-opt', 'cache-disabled=true',
                              '--label', 'com.docker.compose.project=platform-edge-proof-' + marker,
                              '--label', 'com.docker.compose.service=caddy',
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
        query = urllib.parse.urlencode({'query': '{compose_project="platform-edge-proof-' + marker + '",service="caddy"}',
                                        'start': str(timestamp - 60 * 10**9)})
        result = eventually('/api/datasources/proxy/uid/loki/loki/api/v1/query_range?' + query,
                            lambda data: any(row['values'] for row in data['data']['result']))
        assert all(not {'url', 'uri', 'path'}.intersection(row['stream']) for row in result['data']['result'])
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
