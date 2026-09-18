#!/usr/bin/env python3
"""Take or restore a fenced Checkpoint of this installation."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path

import bootstrap

ROOT = Path(__file__).resolve().parent.parent
VOLUMES = ('grafana-data', 'loki-data', 'mimir-data', 'tempo-data', 'alloy-data')
WRITERS = ('caddy', 'alloy', 'grafana', 'loki', 'mimir', 'tempo')


def checked(argv, runner=bootstrap.run):
    result = runner(argv)
    if result.returncode:
        # Compose errors can contain interpolated secrets. Do not echo their output.
        raise RuntimeError(f'{argv[0]} operation failed (exit {result.returncode}); inspect service logs privately')
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


def image_refs():
    return [m[1] for m in re.finditer(r'^    image: (\S+)$', (ROOT / 'compose.yaml').read_text(), re.M)]


def manifest(directory, env_file, mode):
    # Recovery needs the original env through a separate protected channel.
    keys = sorted({m['key'] for m in map(bootstrap.ENV_LINE.match, env_file.read_text().splitlines()) if m})
    return {
        'version': 1, 'timestamp': datetime.now(timezone.utc).isoformat(),
        'images': image_refs(), 'env_keys': keys, 'storage_mode': mode, 'fenced': True,
        'artifacts': inventory(directory),
    }


def configuration_files():
    return [ROOT / name for name in ('compose.yaml', 'compose.s3.yaml', 'config.alloy')] + [
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
        self.config = json.loads(self.dc('config', '--format', 'json'))
        self.project = self.config['name']
        self.image = self.config['services']['caddy']['image']
        self.volumes = VOLUMES + (('rustfs-data',) if self.mode == 's3' else ())
        self.writers = WRITERS + (('rustfs',) if self.mode == 's3' else ())

    def dc(self, *args):
        return checked(self.command + list(args), self.runner)

    def helper(self, mounts, script, *args):
        return checked(['docker', 'run', '--rm', '--network', 'none',
                        '--user', '0', *mounts, '--entrypoint', 'sh', self.image,
                        '-ec', script, 'sh', *args], self.runner)

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
            if container['Config']['Image'] != expected['image']:
                raise RuntimeError('backup requires the checkout pins matching the running services')
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
            except RuntimeError as error:
                raise RuntimeError(f'restore refused: non-empty or unreadable volume {name}') from error

    def start(self):
        bootstrap.write_versions(ROOT, ROOT / 'compose.yaml', self.settings)
        (self.state / 'textfile').mkdir(parents=True, exist_ok=True)
        bootstrap.write_provisioning(ROOT, self.state, self.settings)
        bootstrap.ensure_volumes(self.runner, self.settings.get('OB_VOLUME_PREFIX') or bootstrap.PROJECT,
                                 self.project, self.mode == 's3')
        bootstrap.ensure_network(self.runner, self.settings.get('OB_PLATFORM_NETWORK', 'platform'))
        self.dc('up', '-d', '--wait', '--wait-timeout', '300')
        self.wait_ready()

    def wait_ready(self):
        origin = bootstrap.local_origin(self.settings)
        for service in ('grafana', 'loki', 'mimir', 'tempo', 'alloy'):
            bootstrap.wait_ready(origin + '/health/' + service, host=self.settings.get('OB_PUBLIC_DOMAIN', 'localhost'))


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
    destination = stack.backups / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    destination.mkdir(mode=0o700)
    stopped = []
    capture_failed = False
    try:
        for service in stack.writers:
            if service == 'tempo':
                # Tempo 3.0's frontend queue cleanup runs every 30s and stops on SIGTERM.
                # With ingress and Grafana fenced, let empty query queues disappear first.
                # See upstream modules/frontend/queue/queue.go (queueCleanupPeriod/stopping).
                sleep(35)
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
        doc = manifest(destination, stack.env_file, stack.mode)
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
        if stopped:
            handlers = {}
            try:
                # A second interruption must not strand the services being resumed.
                for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
                    handlers[signum] = signal.signal(signum, signal.SIG_IGN)
                try:
                    stack.dc('start', *reversed(stopped))
                    stack.wait_ready()
                except BaseException:
                    if not capture_failed:
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
    if (doc['version'] != 1 or doc['images'] != image_refs() or not doc['fenced']
            or doc['storage_mode'] != stack.mode):
        raise RuntimeError('Checkpoint format, pins, fence or storage mode differs')
    if doc['artifacts'] != inventory(source):
        raise RuntimeError('Checkpoint checksum or size mismatch')
    if (source / 'installation/storage-mode').read_text().strip() != stack.mode:
        raise RuntimeError('Checkpoint storage marker differs')
    for path in configuration_files():
        if path.read_bytes() != (source / 'configuration' / path.relative_to(ROOT)).read_bytes():
            raise RuntimeError('restore requires the matching configuration checkout')
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
            'env_repair_required': 'repair managed keys in the original .env',
            'alert_delivery_invalid': 'check alert delivery settings in the original .env',
            'alert_delivery_required': 'configure alert delivery or explicitly select OB_ALERTS=placeholder',
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
        raise RuntimeError('interrupted; resuming fenced services')
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGHUP, interrupted)
    sys.exit(cli())
