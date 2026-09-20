"""Verify public status against a disposable installation owned by smoke.sh."""
import http.client
import json
import ssl
import subprocess
from pathlib import Path

import bootstrap
import status_observer


def check(env_file, settings):
    root = Path(__file__).resolve().parent.parent
    state = Path(settings['OB_STATE_DIR'])
    public = state / 'console/status.json'
    # Fresh inspection, never Docker's health label as a substitute for the probes.
    document = status_observer.observe(root, env_file)
    assert document is not None, 'another observer holds the installation lock'
    rows = {row['id']: row for row in document['components']}
    for name in ('caddy', 'grafana', 'alloy', 'loki', 'mimir', 'tempo'):
        assert rows[name]['state'] == 'healthy', (name, rows[name]['state'])
        assert rows[name].get('observedVersion'), name + ' missing runtime version'
    enabled = 's3' in settings.get('COMPOSE_PROFILES', '').split(',')
    assert rows['rustfs']['state'] == ('healthy' if enabled else 'disabled')
    assert rows['rustfs-init']['state'] == ('healthy' if enabled else 'disabled')
    assert rows['bootstrap']['state'] == 'healthy'
    assert rows['bootstrap']['lastExecutionAt']
    assert document['telemetry'] == 'configured'
    modes = [('http', int(settings['OB_HTTP_PORT']))]
    context = None
    if settings['OB_ACCESS_MODE'] == 'local':
        try:
            certificate = subprocess.run(['docker', 'compose', '--env-file', str(env_file),
                'exec', '-T', 'caddy', 'cat', '/data/caddy/pki/authorities/local/root.crt'],
                check=True, capture_output=True, text=True, timeout=30).stdout
        except subprocess.TimeoutExpired:
            raise AssertionError('public CA retrieval exceeded its deadline') from None
        context = ssl.create_default_context(cadata=certificate)
        modes.append(('https', int(settings['OB_HTTPS_PORT'])))

    def request(scheme, port, method='GET', headers=None):
        connection = (bootstrap.LocalHTTPSConnection('localhost', port=port, timeout=10, context=context)
                      if scheme == 'https' else http.client.HTTPConnection('127.0.0.1', port, timeout=10))
        try:
            connection.request(method, '/status.json', headers={'Host': settings['OB_PUBLIC_DOMAIN'], **(headers or {})})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    original = public.read_bytes()
    try:
        for scheme, port in modes:
            code, headers, body = request(scheme, port)
            assert code == 200 and body == original
            assert headers.get('Content-Type') == 'application/json'
            assert headers.get('Cache-Control') == 'no-store'
            assert headers.get('Date')
            assert request(scheme, port, 'HEAD')[::2] == (200, b'')
            code, headers, body = request(scheme, port, 'POST')
            assert code == 405 and body == b'' and headers.get('Allow') == 'GET, HEAD'
            assert request(scheme, port, headers={
                'Authorization': 'Bearer smoke-unused', 'Cookie': 'session=smoke-unused',
                'If-None-Match': '*', 'If-Modified-Since': 'Thu, 31 Dec 2099 23:59:59 GMT',
                'If-Match': '"never-match"',
                'If-Unmodified-Since': 'Thu, 1 Jan 1970 00:00:00 GMT',
                'If-Range': '"never-match"',
                'Range': 'bytes=0-5'})[::2] == (200, original)
        # Frozen evidence stays frozen, even when the gateway serves it with a fresh Date.
        document['generatedAt'] = '2000-01-01T00:00:00Z'
        document['configurationObservedAt'] = document['generatedAt']
        for row in document['components']:
            row['observedAt'] = document['generatedAt']
        frozen = json.dumps(document).encode()
        public.write_bytes(frozen)
        for scheme, port in modes:
            assert request(scheme, port)[::2] == (200, frozen)
        public.unlink()
        for scheme, port in modes:
            code, headers, body = request(scheme, port)
            assert code == 404 and body == b''
            assert headers.get('Content-Type') == 'application/json' and headers.get('Cache-Control') == 'no-store'
    finally:
        public.write_bytes(original)
        public.chmod(0o644)
    print('ok: actual service status and versions, task records, public GET/HEAD/405/404, credentials, frozen age', flush=True)
