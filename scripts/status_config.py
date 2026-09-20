"""Effective Compose selection and conservative configuration evidence."""

import hashlib
import os
import re
from pathlib import Path

from status_io import Unavailable, read_file, read_json

LIMIT = 1024 * 1024
# Reviewed configuration content, not a search for words that could occur in comments.
ALLOY_SHA256 = '4cc892a74c03a33ca988391b0ec343fc52b452a0800cd2356fdbb674528aac4c'
FILESYSTEM_SHA256 = {
    'loki': '0aea8d06f5db820a085094aa128ad19e79a8bb689a93a8f0ce7b4efd1ab7e2c4',
    'mimir': '4eecf3023b461037265cc3f7e14426ead297bddc00c2a7c93ff814a49b641237',
    'tempo': 'c1aee59fb7f27fa5051f0d551075e544403bb981b80a32ee94db879babd5f498',
}


def environment():
    # Native Compose expansion still applies within the selected env file. Interactive
    # shell and systemd-manager stack overrides must not silently select another stack.
    allowed = {'PATH', 'HOME', 'USER', 'XDG_CONFIG_HOME', 'XDG_RUNTIME_DIR', 'SSH_AUTH_SOCK'}
    return {key: value for key, value in os.environ.items()
            if key in allowed or key.startswith('DOCKER_')}


def configuration(root, env_file, runner):
    doc = read_json(runner(['docker', 'compose', '--project-directory', str(root),
                           '--env-file', str(env_file), 'config', '--format', 'json'],
                          timeout=10, limit=LIMIT), LIMIT)
    if (not isinstance(doc, dict) or not isinstance(doc.get('services'), dict)
            or not isinstance(doc.get('name'), str)
            or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,127}', doc['name'])
            or not all(isinstance(value, dict) for value in doc['services'].values())):
        raise Unavailable()
    return doc


def mount(service, target):
    matches = [item for item in service.get('volumes', [])
               if isinstance(item, dict) and item.get('target') == target]
    if (len(matches) != 1 or matches[0].get('type') != 'bind'
            or matches[0].get('read_only') is not True
            or not isinstance(matches[0].get('source'), str)
            or not Path(matches[0]['source']).is_absolute()):
        raise Unavailable()
    return Path(matches[0]['source'])


def console_path(config):
    path = mount(config['services'].get('caddy', {}), '/srv/state')
    if path.name != 'console':
        raise Unavailable()
    return path


def telemetry(config):
    try:
        service = config['services'].get('alloy', {})
        if service.get('entrypoint') is not None:
            return 'unknown'
        if service.get('command') != ['run', '--server.http.listen-addr=0.0.0.0:12345',
                                      '--storage.path=/var/lib/alloy', '/etc/alloy/config.alloy']:
            return 'unknown'
        source = mount(service, '/etc/alloy/config.alloy')
        if hashlib.sha256(read_file(source)).hexdigest() == ALLOY_SHA256:
            return 'configured'
    except (OSError, Unavailable, TypeError, AttributeError):
        pass
    return 'unknown'


def filesystem_off(config, state_dir):
    try:
        if any(name in config['services'] for name in ('rustfs', 'rustfs-init')):
            return False
        if read_file(state_dir / 'installation/storage-mode', 32).strip() != b'filesystem':
            return False
        for name, digest in FILESYSTEM_SHA256.items():
            service = config['services'].get(name, {})
            command = service.get('command', [])
            expected = [f'-config.file=/etc/{name}/config.yaml', '-config.expand-env=true']
            if name != 'loki':
                expected.insert(0, '-target=all')
            if command != expected or service.get('entrypoint') is not None:
                return False
            source = mount(service, f'/etc/{name}/config.yaml')
            if hashlib.sha256(read_file(source)).hexdigest() != digest:
                return False
        return True
    except (OSError, Unavailable, TypeError, AttributeError):
        return False


def local_bridge(config, runner, env):
    """Remote/rootless contexts cannot justify dialing container IPs on this host."""
    try:
        explicit = env.get('DOCKER_HOST')
        if explicit and not explicit.startswith('unix:///'):
            return None
        contexts = read_json(runner(['docker', 'context', 'inspect'], timeout=4, limit=65536))
        endpoint = contexts[0]['Endpoints']['docker']['Host']
        if not endpoint.startswith('unix:///'):
            return None
        if explicit and endpoint != explicit:
            return None
        security = read_json(runner(['docker', 'info', '--format', '{{json .SecurityOptions}}'],
                                    timeout=4, limit=65536))
        if not isinstance(security, list) or any('rootless' in str(item) for item in security):
            return None
        network = config['networks']['default']['name']
        details = read_json(runner(['docker', 'network', 'inspect', network], timeout=4, limit=65536))
        if len(details) == 1 and details[0]['Driver'] == 'bridge' and details[0]['Scope'] == 'local':
            return network
    except (Unavailable, KeyError, TypeError, ValueError, AttributeError, IndexError):
        pass
    return None
