#!/usr/bin/env python3
"""Publish public status for an explicitly selected checkout and env file."""

import argparse
import fcntl
import ipaddress
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from status_config import (LIMIT, configuration, console_path, environment,
                           filesystem_off, local_bridge, telemetry)
from status_io import Unavailable, directory, now, publish, read_json, read_task, regular, run
from status_probes import PATTERNS, configured_image, probe

SERVICES = tuple(PATTERNS)
TASKS = ('bootstrap', 'rustfs-init')
TTL = 120


def timestamp(value, at):
    if not isinstance(value, str) or not re.fullmatch(
            r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z', value):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        ceiling = datetime.fromisoformat(at.replace('Z', '+00:00'))
        if parsed.year < 1970 or (parsed - ceiling).total_seconds() > 5:
            return None
        return parsed.isoformat().replace('+00:00', 'Z')
    except ValueError:
        return None


def empty(component):
    result = {'id': component, 'kind': 'task' if component in TASKS else 'service',
              'configured': None, 'state': 'unknown', 'observedAt': None,
              'validForSeconds': TTL}
    if component in TASKS:
        result['lastExecutionAt'] = None
    return result


def inventory(project, runner):
    text = runner(['docker', 'ps', '--all', '--no-trunc', '--filter',
                   'label=com.docker.compose.project=' + project,
                   '--format', '{{.ID}} {{.Label "com.docker.compose.service"}} {{.Label "com.docker.compose.oneoff"}}'],
                  timeout=4, limit=65536)
    result = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) != 3 or not re.fullmatch(r'[0-9a-f]{64}', fields[0]):
            raise Unavailable()
        container, service, oneoff = fields
        if oneoff.lower() == 'true':
            continue
        if oneoff.lower() != 'false':
            raise Unavailable()
        result.setdefault(service, []).append(container)
    return result


def inspect(container, component, config, runner):
    docs = read_json(runner(['docker', 'inspect', container], timeout=4, limit=LIMIT), LIMIT)
    if not isinstance(docs, list) or len(docs) != 1:
        raise Unavailable()
    doc = docs[0]
    labels = doc['Config']['Labels']
    if (doc['Id'] != container or labels['com.docker.compose.project'] != config['name']
            or labels['com.docker.compose.service'] != component
            or str(labels.get('com.docker.compose.oneoff', '')).lower() != 'false'):
        raise Unavailable()
    return doc


def observe_service(component, candidates, config, inventory_at, network, runner, clock):
    row = empty(component)
    service = config['services'].get(component)
    if service is None:
        return row
    row['configured'] = True
    if component in SERVICES:
        row.update(configured_image(component, service.get('image')))
    if candidates is None or len(candidates) > 1:
        return row
    if not candidates:
        if component not in TASKS:
            row.update(state='absent', observedAt=inventory_at)
        return row
    container = candidates[0]
    # One conservative timestamp covers this bounded inspection and probe transaction.
    at = clock()
    try:
        doc = inspect(container, component, config, runner)
        state = doc['State']
        if component in TASKS:
            started = timestamp(state.get('StartedAt'), at)
            if not started:
                return row
            row.update(observedAt=at, lastExecutionAt=started)
            if state.get('Status') == 'running' and not state.get('Paused'):
                row['state'] = 'starting'
            elif (state.get('Status') == 'exited' and timestamp(state.get('FinishedAt'), at)
                  and datetime.fromisoformat(timestamp(state['FinishedAt'], at)) >= datetime.fromisoformat(started)
                  and type(state.get('ExitCode')) is int):
                row['state'] = 'healthy' if state['ExitCode'] == 0 else 'unavailable'
            return row
        if re.fullmatch(r'sha256:[0-9a-f]{64}', str(doc.get('Image', ''))):
            row['observedImageId'] = doc['Image']
        row['observedAt'] = at
        if state.get('Status') in ('exited', 'dead') or state.get('Paused') is True:
            row['state'] = 'unavailable'
        elif state.get('Status') == 'restarting':
            row['state'] = 'starting'
        elif state.get('Status') == 'running' and network:
            ip = doc.get('NetworkSettings', {}).get('Networks', {}).get(network, {}).get('IPAddress')
            if ip:
                address = ipaddress.ip_address(ip)
                if address.is_loopback or address.is_unspecified or address.is_multicast or address.is_link_local:
                    raise Unavailable()
            host = config['services'].get('caddy', {}).get('environment', {}).get('OB_PUBLIC_DOMAIN', 'localhost')
            if not isinstance(host, str) or not re.fullmatch(r'[a-zA-Z0-9.-]{1,253}', host):
                raise Unavailable()
            row['state'], release = probe(component, container, ip, host, runner)
            if release:
                row['observedVersion'] = release
            after = inspect(container, component, config, runner)
            if (after.get('Image') != doc.get('Image') or
                    any(after['State'].get(key) != state.get(key)
                        for key in ('StartedAt', 'Status', 'Paused'))):
                raise Unavailable()
        return row
    except (Unavailable, KeyError, TypeError, ValueError, AttributeError, OSError):
        row.pop('observedImageId', None)
        row.pop('observedVersion', None)
        row.update(state='unknown', observedAt=None)
        return row


def collect(root, env_file, config, configured_at, state_dir, runner, clock=now):
    rows = {name: empty(name) for name in (*SERVICES, *TASKS)}
    # Config evidence is read before runtime work and shares only its own timestamp.
    telemetry_state = telemetry(config)
    disabled = filesystem_off(config, state_dir)
    inventory_at = clock()
    try:
        resources = inventory(config['name'], runner) or None
    except Unavailable:
        resources = None
    network = local_bridge(config, runner, environment()) if resources is not None else None
    names = (*SERVICES, 'rustfs-init')
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {name: pool.submit(observe_service, name,
                                    resources.get(name, []) if resources is not None else None,
                                    config, inventory_at, network, runner, clock) for name in names}
        for name, future in futures.items():
            try:
                rows[name] = future.result()
            except Exception:
                # A malformed component must never suppress its valid siblings.
                pass
    if disabled:
        for name in ('rustfs', 'rustfs-init'):
            rows[name].update(configured=False, state='disabled', observedAt=configured_at)
    rows['bootstrap']['configured'] = True
    try:
        at = clock()
        record = read_task(state_dir, root, env_file)
        started = timestamp(record.get('lastExecutionAt'), at)
        # An interrupted execution is unknown. A saved PID cannot prove it is running.
        if started and record.get('state') in ('healthy', 'unavailable', 'unknown'):
            rows['bootstrap'].update(state=record['state'], observedAt=at, lastExecutionAt=started)
    except (OSError, Unavailable, TypeError, ValueError):
        pass
    return {'schemaVersion': 1, 'stack': 'observability', 'generatedAt': clock(),
            'configurationObservedAt': configured_at, 'configurationValidForSeconds': TTL,
            'telemetry': telemetry_state, 'components': list(rows.values())}


def observe(root, env_file, runner=run, clock=now):
    root, env_file = Path(root).absolute(), Path(env_file).absolute()
    if not (root / 'compose.yaml').is_file() or not env_file.is_file():
        raise Unavailable()
    env = environment()

    def selected(argv, **options):
        return runner(argv, cwd=root, env=env, **options)

    # Failed configuration cannot safely select a public mount. Leave the old file
    # untouched so its observations expire, rather than guessing a default data path.
    configured_at = clock()
    config = configuration(root, env_file, selected)
    console = console_path(config)
    with directory(console.parent / 'status', 0o700) as private:
        regular(private, 'observer.lock')
        lock = os.open('observer.lock', os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW,
                       0o600, dir_fd=private)
        try:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return None  # Another invocation already owns publication.
            document = collect(root, env_file, config, configured_at, console.parent, selected, clock)
            if len(document['components']) > 32:
                raise Unavailable()
            with directory(console) as public:
                publish(public, 'status.json', document)
        finally:
            os.close(lock)
    return document


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--checkout', type=Path, required=True)
    parser.add_argument('--env-file', type=Path, required=True)
    args = parser.parse_args()
    try:
        observe(args.checkout, args.env_file)
    except (OSError, Unavailable, ValueError, TypeError):
        print('status observer: observation or publication failed', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
