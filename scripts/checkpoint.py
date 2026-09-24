#!/usr/bin/env python3
"""Take or restore a fenced Checkpoint of this installation."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import bootstrap

ROOT = Path(__file__).resolve().parent.parent
VOLUMES = ('grafana-data', 'loki-data', 'mimir-data', 'tempo-data', 'alloy-data')
WRITERS = ('caddy', 'alloy', 'grafana', 'loki', 'mimir', 'tempo')


class Interrupted(RuntimeError):
    pass


class CleanupFailed(RuntimeError):
    pass


def checked(argv, runner=bootstrap.run):
    result = runner(argv)
    if result.returncode:
        # Compose errors can contain interpolated secrets. Do not echo their output.
        tool = 'docker' if argv[0] == 'env' and 'docker' in argv else argv[0]
        raise RuntimeError(f'{tool} operation failed (exit {result.returncode}); inspect service logs privately')
    return result.stdout.strip()


def inventory(directory):
    result = {}
    for path in sorted(directory.rglob('*')):
        if path.is_symlink():
            raise RuntimeError('Checkpoint contains a symlink')
        if not path.is_file() or path == directory / 'manifest.json':
            continue
        digest = hashlib.sha256()
        with path.open('rb') as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b''):
                digest.update(block)
        result[path.relative_to(directory).as_posix()] = {
            'size': path.stat().st_size, 'sha256': digest.hexdigest(),
        }
    return result


def image_repository(ref):
    name = ref.split('@', 1)[0]
    if ':' in name.rsplit('/', 1)[-1]:
        name = name.rsplit(':', 1)[0]
    return name.removeprefix('docker.io/').removeprefix('index.docker.io/').removeprefix('library/')


def immutable_ref(ref):
    if not isinstance(ref, str) or not re.fullmatch(r'[a-z0-9][a-zA-Z0-9./:_-]*@sha256:[0-9a-f]{64}', ref):
        raise RuntimeError('Checkpoint requires a reproducible immutable image reference')
    name, digest = ref.split('@')
    if ':' in name.rsplit('/', 1)[-1]:
        name = name.rsplit(':', 1)[0]
    return name + '@' + digest


def manifest(directory, env_file, mode, images):
    # Recovery needs the original env through a separate protected channel.
    keys = sorted({m['key'] for m in map(bootstrap.ENV_LINE.match, env_file.read_text().splitlines()) if m})
    return {
        'version': 2, 'timestamp': datetime.now(timezone.utc).isoformat(),
        'images': images, 'imageCustody': 'external', 'env_keys': keys, 'storage_mode': mode, 'fenced': True,
        'artifacts': inventory(directory),
    }


def configuration_files():
    return [ROOT / name for name in ('compose.yaml', 'compose.s3.yaml', 'compose.proxy.yaml', 'config.alloy')] + [
        path for path in (ROOT / 'docker').rglob('*') if path.is_file()]


class Stack:
    def __init__(self, env_file, runner=bootstrap.run):
        self.runner = runner
        self.env_file = env_file.resolve()
        lines, secrets = bootstrap.read_env(self.env_file)
        if set(secrets) != bootstrap.MANAGED:
            raise RuntimeError('restore the original .env with all secrets before proceeding')
        settings = {m['key']: bootstrap.unquote(m['value']) for m in map(bootstrap.ENV_LINE.match, lines) if m}
        if any(key in os.environ and os.environ[key] != value for key, value in secrets.items()):
            raise RuntimeError('shell secrets differ from the original .env')
        settings.update({k: v for k, v in os.environ.items() if k.startswith('OB_') or k == 'COMPOSE_PROFILES'})
        self.settings = settings
        self.mode = 's3' if 's3' in settings.get('COMPOSE_PROFILES', '').split(',') else 'filesystem'
        self.backups = (ROOT / settings.get('OB_BACKUP_DIR', './data/backups')).resolve()
        self.state = (ROOT / settings.get('OB_STATE_DIR', './data')).resolve()
        if not self.backups.is_dir():
            raise RuntimeError('OB_BACKUP_DIR must exist; verify its storage is mounted')
        self.command = ['docker', 'compose', '--project-directory', str(ROOT), '--env-file', str(self.env_file),
                        '-f', str(ROOT / 'compose.yaml')]
        if self.mode == 's3':
            self.command += ['-f', str(ROOT / 'compose.s3.yaml'), '--profile', 's3']
        if settings.get('OB_ACCESS_MODE') == 'proxy':
            self.command += ['-f', str(ROOT / 'compose.proxy.yaml')]
        self.config = json.loads(self.dc('config', '--format', 'json'))
        self.project = self.config['name']
        self.image = None
        self.volumes = VOLUMES + (('rustfs-data',) if self.mode == 's3' else ())
        self.writers = WRITERS + (('rustfs',) if self.mode == 's3' else ())

    def dc(self, *args):
        command = self.command + list(args)
        if args[0] == 'up' and getattr(self, 'image_overrides', None):
            command = ['env', *[f'{key}={ref}' for key, ref in self.image_overrides.items()], *command]
        return checked(command, self.runner)

    def resolve_images(self, captured=None):
        if captured is not None and (not isinstance(captured, dict) or set(captured) != set(self.config['services'])):
            raise RuntimeError('Checkpoint image service set differs')
        refs, ids = {}, {}
        for service, config in self.config['services'].items():
            ref = config['image']
            def inspect(reference):
                probe = self.runner(['docker', 'image', 'inspect', reference])
                if probe.returncode:
                    raise RuntimeError(f'{service}: image unavailable locally; pull or load the exact '
                                       'configured or checkpoint image before capture or restore')
                return json.loads(probe.stdout)[0]
            local = inspect(ref)
            identity = local['Id']
            if not re.fullmatch(r'sha256:[0-9a-f]{64}', identity):
                raise RuntimeError('invalid local image identity')
            if captured is not None:
                try:
                    immutable_ref(captured[service])
                except RuntimeError:
                    raise RuntimeError(f'{service}: Checkpoint manifest records a malformed image reference; '
                                       'recover an intact manifest with immutable name@sha256 references') from None
            candidates = ([captured[service]] if captured is not None else [ref] if '@' in ref else
                sorted(local.get('RepoDigests') or [],
                       key=lambda value: (image_repository(value) != image_repository(ref), value)))
            for candidate in candidates:
                try:
                    candidate = immutable_ref(candidate)
                except RuntimeError:
                    continue
                verified = inspect(candidate)
                if verified['Id'] == identity:
                    refs[service], ids[service] = candidate, identity
                    break
            else:
                if captured is not None:
                    raise RuntimeError(f'{service}: configured image content differs from the captured '
                                       'Checkpoint reference; set the matching OB_*_IMAGE to the '
                                       'reference this Checkpoint recorded for the service')
                raise RuntimeError(f'{service}: Checkpoint requires locally verified RepoDigests; '
                                   'publish and pull the exact experiment or select a digest reference before capture')
        self.images, self.image_ids = refs, ids
        self.image = ids.get('caddy')
        # Keep resume/restore on the attested content even if a tag moves afterward.
        self.image_overrides = {
            'OB_' + service.removesuffix('-init').upper() + '_IMAGE': ref
            for service, ref in refs.items()
            if '@' not in self.config['services'][service]['image']
        }

    def helper(self, mounts, script, *args, network='none', timeout=None):
        if self.image is None:
            self.resolve_images()
        deadline = time.monotonic() + timeout if timeout is not None else None
        token = uuid.uuid4().hex
        name = f'{self.project}-checkpoint-{token}'
        label = 'observability.checkpoint.helper'
        pending = []
        handlers = {}
        primary = None
        cleanup_error = None
        runner = self.runner
        if runner is bootstrap.run:
            runner = lambda argv: subprocess.run(argv, text=True, capture_output=True,
                                                 check=False, start_new_session=True)
        def defer(signum, frame):
            pending.append(signum)
        try:
            # Finish create before honoring interruption so cleanup owns a known container.
            for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
                handlers[signum] = signal.signal(signum, defer)
            checked(['docker', 'create', '--pull', 'never', '--name', name, '--label', f'{label}={token}',
                     '--network', network, '--user', '0', *mounts, '--entrypoint', 'sh', self.image,
                     '-ec', script, 'sh', *args], runner)
            for signum, handler in handlers.items():
                signal.signal(signum, handler)
            if pending:
                raise Interrupted('interrupted; cleaning up Checkpoint helper')
            start_runner = runner
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError('Checkpoint helper deadline expired')
                if self.runner is bootstrap.run:
                    start_runner = lambda argv: subprocess.run(
                        argv, text=True, capture_output=True, check=False,
                        start_new_session=True, timeout=remaining)
            return checked(['docker', 'start', '-a', name], start_runner)
        except BaseException as error:
            primary = error
            raise
        finally:
            try:
                for signum in handlers:
                    signal.signal(signum, defer)
            finally:
                try:
                    result = runner(['docker', 'inspect', name])
                    if result.returncode:
                        # Distinguish an absent helper from an unavailable daemon.
                        remaining = checked(['docker', 'ps', '-aq', '--filter', f'name=^/{name}$'], runner)
                        if remaining:
                            raise RuntimeError('cannot inspect Checkpoint helper for cleanup')
                    else:
                        containers = json.loads(result.stdout)
                        if len(containers) != 1 or containers[0]['Config']['Labels'].get(label) != token:
                            raise RuntimeError('Checkpoint helper ownership mismatch; cleanup refused')
                        checked(['docker', 'rm', '-f', containers[0]['Id']], runner)
                except BaseException as error:
                    cleanup_error = error
                    print('FAIL: Checkpoint helper cleanup failed; inspect containers privately', file=sys.stderr)
                finally:
                    for signum, handler in handlers.items():
                        signal.signal(signum, handler)
            if pending and not isinstance(primary, (Interrupted, KeyboardInterrupt)):
                raise Interrupted('interrupted; Checkpoint helper cleanup attempted') from primary
            if cleanup_error is not None and not isinstance(primary, (Interrupted, KeyboardInterrupt)):
                message = str(primary) if primary is not None else 'Checkpoint helper cleanup failed'
                raise CleanupFailed(message) from (primary if primary is not None else cleanup_error)

    def quiesce_tempo(self, timeout=120, sleep=time.sleep):
        deadline = time.monotonic() + timeout
        quiet_since = None
        previous_id = None
        queue = 'tempo_query_frontend_queue_length'
        clients = 'tempo_query_frontend_connected_clients'
        runner = self.runner
        if runner is bootstrap.run:
            runner = lambda argv: subprocess.run(
                argv, text=True, capture_output=True, check=False, start_new_session=True,
                timeout=max(0.01, deadline - time.monotonic()))
        while time.monotonic() < deadline:
            try:
                ids = checked(self.command + ['ps', '-q', 'tempo'], runner).split()
                if len(ids) != 1:
                    raise RuntimeError('Tempo quiescence requires one running container')
                containers = json.loads(checked(['docker', 'inspect', *ids], runner))
                if len(containers) != 1:
                    raise RuntimeError('Tempo quiescence requires one container')
                container = containers[0]
                labels = container['Config']['Labels']
                if (labels.get('com.docker.compose.project') != self.project
                        or labels.get('com.docker.compose.service') != 'tempo'
                        or not container['State']['Running']):
                    raise RuntimeError('Tempo quiescence container ownership/state differs')
                if container['Id'] != previous_id:
                    quiet_since = None
                    previous_id = container['Id']
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                metrics = self.helper([], 'wget -q -T 5 -O - http://127.0.0.1:3200/metrics',
                                      network='container:' + container['Id'], timeout=remaining)
                values = {queue: [], clients: []}
                for line in metrics.splitlines():
                    for name in values:
                        if line.startswith(name):
                            match = re.fullmatch(re.escape(name) + r'(?:\{.*\})?\s+(\S+)', line)
                            if not match or not math.isfinite(value := float(match[1])):
                                raise ValueError('invalid Tempo metric sample')
                            values[name].append(value)
                # GaugeVec has no samples before the first query. The GaugeFunc confirms
                # this is a live frontend; queue length does not count executing queries.
                empty = (len(values[clients]) == 1 and values[clients][0] > 0
                         and all(value == 0 for value in values[queue]))
                now = time.monotonic()
                if empty:
                    if quiet_since is None:
                        quiet_since = now
                    if now - quiet_since >= 35 and now < deadline:
                        return
                else:
                    quiet_since = None
            except (Interrupted, CleanupFailed):
                raise
            except (RuntimeError, ValueError, KeyError, subprocess.TimeoutExpired):
                quiet_since = None
            sleep(min(1, max(0, deadline - time.monotonic())))
        raise RuntimeError('Tempo query quiescence deadline expired; Tempo was not stopped')

    def stopped_cleanly(self, service):
        ids = self.dc('ps', '-aq', service).split()
        if not ids:
            raise RuntimeError(f'fence failed: {service} has no container')
        containers = json.loads(checked(['docker', 'inspect', *ids], self.runner))
        for container in containers:
            state = container['State']
            if state['Status'] != 'exited' or state['ExitCode'] != 0 or state['OOMKilled']:
                raise RuntimeError(f'fence failed: {service} did not stop cleanly; no completed Checkpoint')

    def volume_name(self, key):
        return self.config['volumes'][key]['name']

    def attest_capture(self):
        self.resolve_images()
        ids = self.dc('ps', '-q', *self.writers).split()
        if not ids:
            raise RuntimeError('backup requires the complete stack running')
        containers = json.loads(checked(['docker', 'inspect', *ids], self.runner))
        services = []
        for container in containers:
            labels = container['Config']['Labels']
            service = labels.get('com.docker.compose.service')
            if labels.get('com.docker.compose.project') != self.project or service not in self.writers:
                raise RuntimeError('backup container ownership differs from resolved Compose')
            services.append(service)
            expected = self.config['services'][service]
            if container['Image'] != self.image_ids[service]:
                raise RuntimeError('backup requires configured image identity matching the running services')
            mounts = set()
            for mount in expected.get('volumes', []):
                kind = mount['type']
                source = self.volume_name(mount['source']) if kind == 'volume' else mount['source']
                if kind == 'bind':
                    source = str(Path(source).resolve())
                mounts.add((kind, source, mount['target'], not mount.get('read_only', False)))
            actual = {(mount['Type'], mount.get('Name') if mount['Type'] == 'volume'
                       else str(Path(mount['Source']).resolve()), mount['Destination'], mount['RW'])
                      for mount in container['Mounts']}
            if mounts != actual:
                raise RuntimeError(f'backup requires {service} mounts matching resolved Compose')
        if sorted(services) != sorted(self.writers):
            raise RuntimeError('backup requires exactly one container for each fenced service')
        self.check_capture_volumes({container['Id'] for container in containers})

    def check_capture_volumes(self, allowed_consumers=()):
        # --mount creates absent volumes. Inspect first and reject independent writers.
        names = {self.volume_name(key) for key in self.volumes}
        names.update(self.volume_name(mount['source']) for service in self.writers
                     for mount in self.config['services'][service].get('volumes', [])
                     if mount['type'] == 'volume')
        for name in sorted(names):
            checked(['docker', 'volume', 'inspect', name], self.runner)
            consumers = checked(['docker', 'ps', '--no-trunc', '-q', '--filter', f'volume={name}'],
                                self.runner).split()
            if set(consumers) - set(allowed_consumers):
                raise RuntimeError(f'backup refused: unexpected running consumer of {name}')

    def check_empty(self):
        if checked(['docker', 'ps', '-q', '--filter', f'label=com.docker.compose.project={self.project}'], self.runner):
            raise RuntimeError('restore requires all project containers stopped')
        installation = self.state / 'installation'
        if installation.exists() and any(installation.iterdir()):
            raise RuntimeError('restore refused: non-empty installation marker directory')
        names = checked(['docker', 'volume', 'ls', '--format', '{{.Name}}'], self.runner).split()
        labelled = checked(['docker', 'volume', 'ls', '-q', '--filter',
                            f'label=com.docker.compose.project={self.project}'], self.runner).split()
        targets = set(labelled) | {name for name in names if name.startswith(self.project + '_')}
        targets |= {volume['name'] for volume in self.config['volumes'].values()} & set(names)
        for name in sorted(targets):
            if checked(['docker', 'ps', '-q', '--filter', f'volume={name}'], self.runner):
                raise RuntimeError(f'restore refused: running consumer of target volume {name}')
            try:
                self.helper(['--mount', f'type=volume,src={name},dst=/target,readonly'],
                            'entries=$(ls -A /target); test -z "$entries"')
            except (Interrupted, CleanupFailed):
                raise
            except RuntimeError as error:
                raise RuntimeError(f'restore refused: non-empty or unreadable volume {name}') from error

    def start(self):
        bootstrap.write_versions(ROOT, ROOT / 'compose.yaml', self.settings, services=self.config['services'])
        (self.state / 'textfile').mkdir(parents=True, exist_ok=True)
        bootstrap.write_provisioning(ROOT, self.state, self.settings)
        bootstrap.ensure_volumes(self.runner, self.settings.get('OB_VOLUME_PREFIX') or bootstrap.PROJECT,
                                 self.project, self.mode == 's3')
        bootstrap.ensure_network(self.runner, self.settings.get('OB_PLATFORM_NETWORK', 'platform'))
        self.dc('up', '-d', '--wait', '--wait-timeout', '300')
        self.wait_ready()

    def wait_ready(self, restart=False, timeout=120, settle=0):
        settle_until = time.monotonic() + settle
        deadline = settle_until + timeout
        origin = bootstrap.local_origin(self.settings)
        last = RuntimeError('service resumption timed out; inspect service state privately')
        probes_passed = False
        while time.monotonic() < deadline:
            try:
                ready = True
                for service in reversed(self.writers):
                    ids = self.dc('ps', '-aq', service).split()
                    if not ids:
                        ready = False
                        if restart:
                            self.dc('up', '-d', '--no-deps', service)
                        continue
                    containers = json.loads(checked(['docker', 'inspect', *ids], self.runner))
                    if len(containers) != 1:
                        raise RuntimeError(f'resumption requires one container for {service}')
                    state = containers[0]['State']
                    if not state.get('Running', False):
                        ready = False
                        if restart:
                            self.dc('start', service)
                        continue
                    healthcheck = self.config['services'][service].get('healthcheck', {})
                    health = state.get('Health', {}).get('Status')
                    required = bool(healthcheck) and not healthcheck.get('disable') and healthcheck.get('test') != ['NONE']
                    if health not in (None, 'healthy') or (required and health != 'healthy'):
                        ready = False
                if ready:
                    if probes_passed and time.monotonic() >= settle_until:
                        return
                    for service in ('grafana', 'loki', 'mimir', 'tempo', 'alloy'):
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise RuntimeError('service readiness deadline expired')
                        bootstrap.wait_ready(origin + '/health/' + service, timeout=min(1, remaining),
                                             host=self.settings.get('OB_PUBLIC_DOMAIN', 'localhost'))
                    # A stop submitted before interruption may finish after the first start.
                    probes_passed = True
                    if not settle:
                        continue
                if not ready:
                    probes_passed = False
            except Interrupted:
                raise
            except (RuntimeError, bootstrap.Refused, subprocess.TimeoutExpired) as error:
                last = error
                probes_passed = False
            time.sleep(min(1, max(0, deadline - time.monotonic())))
        raise RuntimeError('service resumption/readiness deadline expired; inspect service state privately') from last

    def resume(self, stopped, timeout=120, settle=0):
        runner = self.runner
        started = time.monotonic()
        settle_until = started + settle
        deadline = started + timeout + settle
        if runner is bootstrap.run:
            self.runner = lambda argv: subprocess.run(
                argv, text=True, capture_output=True, check=False, start_new_session=True,
                timeout=max(0.01, deadline - time.monotonic()))
        try:
            # Start can fail while an earlier stop is still in flight; polling retries it.
            try:
                self.dc('start', *reversed(stopped))
            except Interrupted:
                raise
            except (RuntimeError, subprocess.TimeoutExpired):
                pass
            now = time.monotonic()
            remaining_settle = max(0, settle_until - now)
            self.wait_ready(restart=True, timeout=max(0, deadline - now - remaining_settle),
                            settle=remaining_settle)
        finally:
            self.runner = runner


def complete_checkpoints(directory):
    return sorted(path for path in directory.iterdir()
                  if path.is_dir() and not path.is_symlink()
                  and re.fullmatch(r"[0-9]{8}T[0-9]{12}Z", path.name)
                  and (path / 'manifest.json').is_file())


def check_backup_space(directory):
    checkpoints = complete_checkpoints(directory)
    if checkpoints:
        size = sum(path.stat().st_size for path in checkpoints[-1].rglob('*') if path.is_file())
        if shutil.disk_usage(directory).free < size:
            raise RuntimeError('backup refused: free space is below the size of the last Checkpoint')


def prune_checkpoints(directory, keep):
    for path in complete_checkpoints(directory)[:-keep]:
        shutil.rmtree(path)


def backup(stack, timeout=120, sleep=time.sleep):
    if timeout < 1:
        raise RuntimeError('--stop-timeout must be a positive integer')
    keep = int(stack.settings.get('OB_BACKUP_KEEP', '7'))
    if keep < 1:
        raise RuntimeError('OB_BACKUP_KEEP must be a positive integer')
    check_backup_space(stack.backups)
    running = set(stack.dc('ps', '--status', 'running', '--services').split())
    if not set(stack.writers) <= running:
        raise RuntimeError('backup requires the complete stack running')
    marker = stack.state / 'installation' / 'storage-mode'
    if not marker.is_file() or marker.read_text().strip() != stack.mode:
        raise RuntimeError('storage-mode marker is missing or differs from .env')
    stack.attest_capture()
    if any('@' not in service['image'] for service in stack.config['services'].values()):
        print('Image custody is external: retain the recorded immutable references in a registry or a tested off-host image archive; publication was not checked.', file=sys.stderr, flush=True)
    destination = stack.backups / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    destination.mkdir(mode=0o700)
    stopped = []
    capture_failed = False
    resuming = False
    interrupted = False
    handlers = {}
    def interrupt(signum, frame):
        nonlocal interrupted
        if not interrupted and not resuming and not capture_failed:
            interrupted = True
            raise Interrupted('interrupted; resuming fenced services')
    try:
        for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            handlers[signum] = signal.signal(signum, interrupt)
        for service in stack.writers:
            if service == 'tempo':
                # Tempo 3.0's 30s queue cleanup timer stops on SIGTERM.
                stack.quiesce_tempo(timeout=max(120, timeout), sleep=sleep)
            stopped.append(service)
            stack.dc('stop', '-t', str(timeout), service)
            stack.stopped_cleanly(service)
        stack.check_capture_volumes()
        for key in stack.volumes:
            stack.helper(['--mount', f'type=volume,src={stack.volume_name(key)},dst=/source,readonly',
                          '--mount', f'type=bind,src={destination},dst=/checkpoint'],
                         'umask 077; tar -C /source -cf "/checkpoint/$1.tar" .; '
                         'chown "$2:$3" "/checkpoint/$1.tar"', key, str(os.getuid()), str(os.getgid()))
        verify_archives(destination, stack.volumes)
        shutil.copytree(marker.parent, destination / 'installation')
        for path in configuration_files():
            target = destination / 'configuration' / path.relative_to(ROOT)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
        # Recheck the fence before publishing a complete set.
        for service in stack.writers:
            stack.stopped_cleanly(service)
        stack.check_capture_volumes()
        doc = manifest(destination, stack.env_file, stack.mode, stack.images)
        for path in destination.rglob('*'):
            if path.is_file():
                with path.open('rb') as handle:
                    os.fsync(handle.fileno())
        temporary = destination / '.manifest.tmp'
        with temporary.open('x') as handle:
            json.dump(doc, handle, indent=2)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        temporary.rename(destination / 'manifest.json')
        directories = [path for path in destination.rglob('*') if path.is_dir()]
        for directory in [*reversed(directories), destination, stack.backups]:
            fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    except BaseException:
        capture_failed = True
        raise
    finally:
        try:
            # The outer finally still resumes if the first signal lands at this transition.
            resuming = True
        finally:
            try:
                if stopped:
                    try:
                        stack.resume(stopped, settle=timeout + 10 if capture_failed or interrupted else 0)
                    except BaseException:
                        if not capture_failed:
                            if (destination / 'manifest.json').is_file():
                                print(f'Checkpoint complete but services not resumed: {destination}', file=sys.stderr)
                            raise
                        print('FAIL: service resumption also failed; inspect service state privately', file=sys.stderr)
            finally:
                for signum, handler in handlers.items():
                    signal.signal(signum, handler)
    # A durable capture remains recoverable even if service resumption fails.
    textfile = stack.state / 'textfile'
    textfile.mkdir(parents=True, exist_ok=True)
    pending = textfile / 'checkpoint.prom.tmp'
    pending.write_text('# TYPE stack_checkpoint_last_success_timestamp_seconds gauge\n'
                       f'stack_checkpoint_last_success_timestamp_seconds{{stack="{stack.project}"}} {time.time():.0f}\n')
    os.chmod(pending, 0o644)
    pending.replace(textfile / 'checkpoint.prom')
    prune_checkpoints(stack.backups, keep)
    print(f'Checkpoint: {destination}', flush=True)
    return destination


def verify_checkpoint(stack, source):
    doc = json.loads((source / 'manifest.json').read_text())
    if (doc['version'] not in (1, 2) or not doc['fenced']
            or doc['storage_mode'] != stack.mode):
        raise RuntimeError('Checkpoint format, pins, fence or storage mode differs')
    if doc['artifacts'] != inventory(source):
        raise RuntimeError('Checkpoint checksum or size mismatch')
    if (source / 'installation/storage-mode').read_text().strip() != stack.mode:
        raise RuntimeError('Checkpoint storage marker differs')
    for path in configuration_files():
        if path.read_bytes() != (source / 'configuration' / path.relative_to(ROOT)).read_bytes():
            raise RuntimeError('restore requires the matching configuration checkout')
    captured = doc['images']
    if doc['version'] == 1:
        defaults = bootstrap.images(ROOT / 'compose.yaml')
        if captured != list(defaults.values()):
            raise RuntimeError('Checkpoint legacy image pins differ')
        captured = {name: defaults[name] for name in stack.config['services']}
    stack.resolve_images(captured)
    verify_archives(source, stack.volumes)
    lines, _ = bootstrap.read_env(stack.env_file)
    present = {match['key'] for match in map(bootstrap.ENV_LINE.match, lines) if match}
    missing = set(doc['env_keys']) - present
    if missing:
        names = sorted(key for key in missing if re.fullmatch(r'[A-Z][A-Z0-9_]*', key))
        print('WARNING: captured env key names missing from supplied .env: ' + ', '.join(names), file=sys.stderr)


def verify_archives(directory, volumes):
    for key in volumes:
        with tarfile.open(directory / (key + '.tar')) as archive:
            for member in archive:
                path = Path(member.name)
                if path.is_absolute() or '..' in path.parts or not (member.isfile() or member.isdir()):
                    raise RuntimeError('unsupported archive member')


def restore(stack, source):
    source = source.resolve()
    verify_checkpoint(stack, source)
    stack.check_empty()
    bootstrap.alert_config(stack.settings)
    bootstrap.ensure_network(stack.runner, stack.settings.get('OB_PLATFORM_NETWORK', 'platform'))
    for key in stack.volumes:
        name = stack.volume_name(key)
        checked(['docker', 'volume', 'create', '--label', f'com.docker.compose.project={stack.project}',
                 '--label', f'com.docker.compose.volume={key}', name], stack.runner)
        stack.helper(['--mount', f'type=volume,src={name},dst=/target',
                      '--mount', f'type=bind,src={source},dst=/checkpoint,readonly'],
                     'tar -xpf "/checkpoint/$1.tar" -C /target', key)
    installation = stack.state / 'installation'
    installation.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source / 'installation', installation, dirs_exist_ok=True)
    if stack.image_overrides:
        print('Retain these verified image overrides in the installation .env before the next Compose update:', flush=True)
        for key, ref in sorted(stack.image_overrides.items()):
            print(f'{key}={ref}', flush=True)
    stack.start()
    print('Restore complete; all five readiness probes passed', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    capture = commands.add_parser('backup')
    capture.add_argument('--stop-timeout', type=int, default=120)
    recover = commands.add_parser('restore')
    recover.add_argument('checkpoint', type=Path)
    for command in (capture, recover):
        command.add_argument('--env-file', type=Path, default=ROOT / '.env')
    args = parser.parse_args()
    if args.command == 'backup' and args.stop_timeout < 1:
        parser.error('--stop-timeout must be a positive integer')
    os.umask(0o077)
    with args.env_file.with_name(args.env_file.name + '.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        stack = Stack(args.env_file)
        with (stack.backups / '.checkpoint.lock').open('w') as repository_lock:
            fcntl.flock(repository_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if args.command == 'backup':
                backup(stack, args.stop_timeout)
            else:
                restore(stack, args.checkpoint)


def cli():
    try:
        main()
        return 0
    except bootstrap.Refused as error:
        # Refused.detail can contain raw Docker output or interpolated settings.
        details = {
            'image_default_unrecognized': 'compose.yaml has an image default this tooling cannot parse',
            'env_repair_required': 'repair managed keys in the original .env',
            'alert_delivery_invalid': 'check alert delivery settings in the original .env',
            'network_create_failed': 'shared network unavailable; inspect Docker diagnostics privately',
            'volume_create_failed': 'volume creation failed; inspect Docker diagnostics privately',
            'not_ready': 'readiness probes failed; inspect service logs privately',
        }
        code = error.code if re.fullmatch(r'[a-z_]+', error.code) else 'bootstrap_refused'
        detail = details.get(code, 'bootstrap prerequisite failed; inspect installation privately')
        print(f'FAIL: {code}: {detail}', file=sys.stderr)
        return 1
    except (RuntimeError, OSError, ValueError, KeyError, tarfile.TarError, KeyboardInterrupt) as error:
        print(f'FAIL: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    def interrupted(signum, frame):
        raise Interrupted('interrupted; resuming fenced services')
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGHUP, interrupted)
    sys.exit(cli())
