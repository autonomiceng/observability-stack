"""Documented service probe responses and public version redaction."""

import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
import status_probes as probes
from status_io import Unavailable, Unsupported


class ProbeTests(unittest.TestCase):
    def runner(self, body='ready', code=200, version='1.2.3', failure=None):
        def run(argv, **options):
            self.assertLessEqual(options['timeout'], 4)
            if argv[0] == sys.executable:
                path = argv[-2]
                self.assertEqual(argv[-1], 'observe.example')
                if path == '/api/health':
                    data = json.dumps({'database': body, 'version': version, 'secret': 'SECRET'})
                elif path == '/status/version':
                    data = 'GET /status/version\ntempo, version ' + version + ' (branch: HEAD, revision: SECRET)\n'
                elif path == '/loki/api/v1/status/buildinfo':
                    data = json.dumps({'version': version, 'secret': 'SECRET'})
                elif path == '/api/v1/status/buildinfo':
                    data = json.dumps({'status': 'success', 'data': {'version': version}})
                else:
                    data = body
                return json.dumps([code, data])
            self.assertEqual(argv[:3], ['docker', 'exec', 'container'])
            self.assertEqual(argv[3:7], ['timeout', '-s', 'KILL', '3'])
            if failure:
                raise failure()
            return {'caddy': 'v' + version + ' h1:privatehash',
                    'alloy': 'alloy, version v' + version + ' (branch: HEAD, revision: private)\n  build user: PRIVATE\n  platform: linux/amd64\n',
                    'rustfs': 'rustfs ' + version}[argv[7]]
        return run

    def test_all_service_successes_use_actual_version_evidence(self):
        for service in probes.PATTERNS:
            with self.subTest(service=service):
                result = probes.probe(service, 'container', '172.20.0.2', 'observe.example',
                                      self.runner(body='ok'))
                self.assertEqual(result, ('healthy', '1.2.3'))

    def test_grafana_requires_database_ok(self):
        self.assertEqual(probes.probe('grafana', 'container', '172.20.0.2', 'observe.example',
                                     self.runner(body='failed')), ('unavailable', None))

    def test_unsupported_health_and_failed_health_are_distinct(self):
        for code, state in ((401, 'unknown'), (403, 'unknown'), (404, 'unknown'),
                            (500, 'unavailable'), (302, 'unavailable')):
            with self.subTest(code=code):
                self.assertEqual(probes.probe('loki', 'container', '172.20.0.2', 'observe.example',
                                             self.runner(code=code)), (state, None))

    def test_missing_version_binary_does_not_undo_health(self):
        for failure in (Unavailable, Unsupported):
            self.assertEqual(probes.probe('alloy', 'container', '172.20.0.2', 'observe.example',
                                         self.runner(failure=failure)), ('healthy', None))

    def test_http_timeout_does_not_become_running_equals_healthy(self):
        with patch.object(probes, 'http', side_effect=Unavailable):
            self.assertEqual(probes.probe('mimir', 'container', '172.20.0.2', 'observe.example'),
                             ('unavailable', None))

    def test_custom_tags_and_runtime_bodies_cannot_leak_secrets(self):
        for service in probes.PATTERNS:
            for tag in ('private-SECRET', '1.2.3-SECRET', '١.٢.٣', '1.2.3\nSECRET'):
                with self.subTest(service=service, tag=tag):
                    result = probes.configured_image(service, 'registry.private/repo:' + tag)
                    self.assertEqual(result, {'configuredVersion': 'custom'})
                    self.assertIsNone(probes.version(service, tag))
        self.assertEqual(probes.configured_image('caddy', 'local:2.11.4@sha256:' + 'a' * 64),
                         {'configuredVersion': '2.11.4', 'configuredDigest': 'sha256:' + 'a' * 64})
        self.assertEqual(probes.configured_image('caddy', 'local:2.11.4-alpine'),
                         {'configuredVersion': '2.11.4'})
        self.assertEqual(probes.configured_image('grafana', 'local:12.1.0-ubuntu'),
                         {'configuredVersion': '12.1.0'})

    def test_probe_has_no_address_no_runtime_claim(self):
        self.assertEqual(probes.probe('rustfs', 'container', None, 'observe.example'), ('unknown', None))

    def test_caddy_wrong_body_is_failure(self):
        state, _ = probes.probe('caddy', 'container', '172.20.0.2', 'observe.example', self.runner(body='HTML'))
        self.assertEqual(state, 'unavailable')

    def test_version_failure_is_independent_of_backend_readiness(self):
        def runner(argv, **options):
            return json.dumps([200, 'ready']) if argv[-2] == '/ready' else json.dumps([200, 'SECRET'])
        self.assertEqual(probes.probe('tempo', 'container', '172.20.0.2', 'observe.example', runner),
                         ('healthy', None))


class HTTPBoundaryTests(unittest.TestCase):
    def test_child_http_uses_no_proxy_credentials_or_redirects(self):
        import os
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading
        seen = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append((self.path, dict(self.headers)))
                self.send_response(302 if self.path == '/redirect' else 200)
                self.send_header('Location', '/secret')
                self.end_headers()
                self.wfile.write(b'ok')

            def log_message(self, *args):
                pass
        try:
            server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        except PermissionError:
            self.skipTest('sandbox forbids loopback sockets; root runtime acceptance required')
        with server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with patch.dict(os.environ, {'HTTP_PROXY': 'http://bad.invalid:1', 'HTTPS_PROXY': 'http://bad.invalid:1'}):
                    self.assertEqual(probes.http('127.0.0.1', server.server_port, '/ready', 'chosen.invalid', probes.run), 'ok')
                    with self.assertRaises(Unavailable):
                        probes.http('127.0.0.1', server.server_port, '/redirect', 'chosen.invalid', probes.run)
            finally:
                server.shutdown()
                thread.join()
        self.assertEqual([path for path, _ in seen], ['/ready', '/redirect'])
        for _, headers in seen:
            self.assertEqual(headers['Host'], 'chosen.invalid')
            self.assertFalse({'Authorization', 'Proxy-Authorization', 'Cookie'} & headers.keys())
