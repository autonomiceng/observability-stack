"""Bounded, credential-free runtime probes; see docs/operations/status.md."""

import ipaddress
import json
import re
import sys
from http.client import HTTPConnection, HTTPException
from pathlib import Path

from status_io import Unavailable, read_json, run

SEMVER = r'\d{1,4}\.\d{1,4}\.\d{1,4}'
PATTERNS = {
    'caddy': rf'v?({SEMVER})(?:-alpine)?',
    'grafana': rf'v?({SEMVER})(?:-ubuntu)?',
    'alloy': rf'v?({SEMVER})',
    'loki': rf'v?({SEMVER})',
    'mimir': rf'v?({SEMVER})',
    'tempo': rf'v?({SEMVER})',
    'rustfs': rf'v?({SEMVER})',
}
ENDPOINTS = {
    'caddy': (80, '/health/status'),
    'grafana': (3000, '/api/health'),
    'alloy': (12345, '/-/ready'),
    'loki': (3100, '/ready'),
    'mimir': (8080, '/ready'),
    'tempo': (3200, '/ready'),
    'rustfs': (9000, '/health/ready'),
}


def version(service, value):
    match = re.fullmatch(PATTERNS[service], value, re.ASCII) if isinstance(value, str) else None
    return match[1] if match else None


def configured_image(service, image):
    if not isinstance(image, str):
        return {}
    reference, _, digest = image.partition('@')
    tag = reference.rsplit('/', 1)[-1].partition(':')[2]
    result = {'configuredVersion': version(service, tag) or 'custom'} if tag else {}
    if re.fullmatch(r'sha256:[0-9a-f]{64}', digest):
        result['configuredDigest'] = digest
    return result


def http(ip, port, path, host, runner):
    # A child process bounds the whole exchange, including trickled HTTP headers.
    ipaddress.ip_address(ip)
    text = runner([sys.executable, str(Path(__file__).resolve()), ip, str(port), path, host],
                  timeout=4, limit=524288)
    response = read_json(text, 524288)
    if (not isinstance(response, list) or len(response) != 2
            or type(response[0]) is not int or not isinstance(response[1], str)):
        raise Unavailable()
    if response[0] in (401, 403, 404):
        return None
    if response[0] != 200:
        raise Unavailable()
    return response[1]


def runtime_version(service, container, ip, host, runner):
    paths = {'loki': '/loki/api/v1/status/buildinfo',
             'mimir': '/api/v1/status/buildinfo', 'tempo': '/status/version'}
    if service in paths:
        body = http(ip, ENDPOINTS[service][0], paths[service], host, runner)
        if body is None:
            return None
        if service == 'tempo':
            # Tempo's status endpoint wraps Prometheus version.Print in plain text.
            match = re.match(r'GET /status/version\r?\ntempo, version (\S+) \([^\r\n]*\)\r?\n', body)
            return version(service, match[1]) if match else None
        data = read_json(body)
        if service == 'mimir' and isinstance(data, dict):
            data = data.get('data') if data.get('status') == 'success' else None
        return version(service, data.get('version')) if isinstance(data, dict) else None
    commands = {'caddy': ['caddy', 'version'], 'alloy': ['alloy', '--version'],
                'rustfs': ['rustfs', '--version']}
    if service not in commands:
        return None
    # The in-container timeout also bounds execution if the Docker client is killed.
    text = runner(['docker', 'exec', container, 'timeout', '-s', 'KILL', '3', *commands[service]],
                  timeout=4, limit=65536).strip()
    if service == 'caddy':
        value = text.split(' ', 1)[0]
    elif service == 'alloy':
        match = re.fullmatch(r'alloy, version (\S+)(?: \([^\r\n]*\))?', text.splitlines()[0] if text else '')
        value = match[1] if match else None
    else:
        value = text.removeprefix('rustfs ')
    return version(service, value)


def probe(service, container, ip, host, runner=run):
    if not ip:
        return 'unknown', None
    state, release = 'unknown', None
    try:
        body = http(ip, *ENDPOINTS[service], host, runner)
        if body is not None:
            if service == 'caddy' and body != 'ok':
                raise Unavailable()
            if service == 'grafana':
                data = read_json(body)
                if not isinstance(data, dict) or data.get('database') != 'ok':
                    raise Unavailable()
                release = version(service, data.get('version'))
            state = 'healthy'
    except Unavailable:
        state = 'unavailable'
    if release is None:
        try:
            release = runtime_version(service, container, ip, host, runner)
        except (Unavailable, ValueError, TypeError):
            pass  # Version failure does not erase a successful readiness observation.
    return state, release


def http_main():
    connection = HTTPConnection(sys.argv[1], int(sys.argv[2]), timeout=3)
    try:
        connection.request('GET', sys.argv[3], headers={
            'Host': sys.argv[4], 'Accept-Encoding': 'identity'})
        response = connection.getresponse()
        body = response.read(65537)
        if len(body) > 65536:
            return 1
        print(json.dumps([response.status, body.decode('utf-8')]))
        return 0
    except (OSError, ValueError, HTTPException):
        return 1
    finally:
        connection.close()


if __name__ == '__main__':
    raise SystemExit(http_main())
