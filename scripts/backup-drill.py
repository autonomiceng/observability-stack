#!/usr/bin/env python3
"""Prove recovery using only a fresh disposable project."""
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import checkpoint
import recovery_assertions


def prepare_checkout(source, target):
    # Keep installation files untouched; restrict collection only in this disposable copy.
    target.mkdir()
    for name in ('scripts', 'docker'):
        shutil.copytree(source / name, target / name, ignore=shutil.ignore_patterns('__pycache__'))
    for name in ('compose.yaml', 'compose.s3.yaml', 'compose.proxy.yaml', '.env.example', 'config.alloy'):
        shutil.copy2(source / name, target / name)
    config = (target / 'config.alloy').read_text()
    start = config.index('discovery.docker "host"')
    end = config.index('discovery.relabel "gateway"')
    config = config[:start] + config[end:]
    start = config.index('prometheus.exporter.cadvisor "containers"')
    end = config.index('prometheus.exporter.unix "textfile"')
    config = config[:start] + config[end:]
    start = config.index('prometheus.exporter.unix "textfile"')
    end = config.index('// Exporter targets', start)
    config = config[:start] + '''prometheus.exporter.unix "textfile" {
  set_collectors = ["textfile"]
  textfile {
    directory = "/var/lib/alloy/textfile"
  }
}

''' + config[end:]
    (target / 'config.alloy').write_text(config)
    compose = (target / 'compose.yaml').read_text()
    host_mounts = ('/var/run/docker.sock:', '/:/rootfs:', '/var/run:', '/sys:', '/dev/disk:', '/var/lib/docker:')
    compose = '\n'.join(line for line in compose.splitlines()
                        if line.strip() != 'privileged: true'
                        and not any(line.strip().startswith('- ' + mount) for mount in host_mounts)) + '\n'
    (target / 'compose.yaml').write_text(compose)
    return target


def profile_settings(profile):
    if profile not in ('filesystem', 's3'):
        raise RuntimeError('SMOKE_PROFILE must be filesystem or s3')
    return {'COMPOSE_PROFILES': 's3' if profile == 's3' else '',
            'COMPOSE_FILE': 'compose.yaml:compose.s3.yaml' if profile == 's3' else 'compose.yaml'}


def cleanup(command, project, network, network_created, work, succeeded):
    failed = False
    def attempt(argv, action):
        nonlocal failed
        try:
            result = checkpoint.bootstrap.run(argv)
            if result.returncode == 0:
                return
        except OSError:
            pass
        failed = True
        print('drill cleanup failed: ' + action, file=sys.stderr)

    attempt(command + ['down', '-v', '--remove-orphans'], 'compose down')
    for key in checkpoint.bootstrap.VOLUMES:
        name = f'{project}_{key}'
        try:
            exists = checkpoint.bootstrap.run(['docker', 'volume', 'inspect', name]).returncode == 0
        except OSError:
            failed = True
            continue
        if exists:
            attempt(['docker', 'volume', 'rm', name], 'remove ' + name)
    if network_created:
        attempt(['docker', 'network', 'rm', network], 'remove network ' + network)
    if succeeded and not failed:
        try:
            shutil.rmtree(work)
        except OSError:
            failed = True
    if not succeeded or failed:
        print(f'drill artifacts retained at {work}', file=sys.stderr)
    return failed


def run_checkpoint(root, operation, env_file, *args):
    # Only checkpoint.py's sanitized stderr is inherited; Docker calls stay captured.
    result = subprocess.run([str(root / 'scripts' / (operation + '.sh')), *args,
                             '--env-file', str(env_file)], stdout=subprocess.PIPE, text=True)
    if result.returncode:
        raise RuntimeError(f'{operation} failed (exit {result.returncode}); see Checkpoint diagnostic above')
    return result.stdout.strip()


def main():
    root = checkpoint.ROOT
    profile = os.environ.get('SMOKE_PROFILE', 'filesystem')
    storage_settings = profile_settings(profile)
    project = os.environ.get('SMOKE_PROJECT', 'observability-drill')
    if not re.fullmatch(r'observability-drill(?:-[a-z0-9-]+)?', project):
        raise RuntimeError('SMOKE_PROJECT must be observability-drill or observability-drill-<suffix>')
    port = os.environ.get('SMOKE_HTTP_PORT', '18190')
    https_port = os.environ.get('SMOKE_HTTPS_PORT', '18553')
    for key in list(os.environ):
        if key.startswith('OB_') or key in ('COMPOSE_FILE', 'COMPOSE_PROFILES', 'COMPOSE_ENV_FILES'):
            os.environ.pop(key)
    os.environ['COMPOSE_PROJECT_NAME'] = project
    run = checkpoint.checked
    containers = run(['docker', 'ps', '-aq', '--filter', f'label=com.docker.compose.project={project}'])
    names = run(['docker', 'volume', 'ls', '--format', '{{.Name}}']).split()
    labelled = run(['docker', 'volume', 'ls', '-q', '--filter', f'label=com.docker.compose.project={project}'])
    if containers or labelled or any(name.startswith(project + '_') for name in names):
        raise RuntimeError('drill project already exists; refusing to touch it')
    network = project + '-platform'
    if checkpoint.bootstrap.run(['docker', 'network', 'inspect', network]).returncode == 0:
        raise RuntimeError('drill network already exists; refusing to touch it')
    work = Path(tempfile.mkdtemp(prefix='observability-drill-'))
    work.chmod(0o755)
    root = prepare_checkout(root, work / 'checkout')
    checkpoint.ROOT = root
    env_file = work / '.env'
    settings = {
        **storage_settings,
        'OB_HTTP_PORT': port, 'OB_HTTPS_PORT': https_port, 'OB_PUBLIC_PORT_SUFFIX': ':' + port,
        'OB_PLATFORM_NETWORK': network, 'OB_STATE_DIR': str(work / 'data'),
        'OB_BACKUP_DIR': str(work / 'backups'), 'OB_SCRAPE_GATEWAY': 'false',
        'OB_VOLUME_PREFIX': project, 'OB_ALERTS': 'placeholder', 'OB_OPERATOR_ALLOW': 'private_ranges',
    }
    text = (root / '.env.example').read_text()
    for key, value in settings.items():
        text = re.sub(rf'^{key}=.*$', f'{key}={value}', text, flags=re.M)
    env_file.write_text(text)
    env_file.chmod(0o600)
    network_created = False
    succeeded = False
    command = ['docker', 'compose', '--project-directory', str(root), '-f', str(root / 'compose.yaml'),
               '--env-file', str(env_file)]
    if profile == 's3':
        command += ['-f', str(root / 'compose.s3.yaml'), '--profile', 's3']
    try:
        run(['docker', 'network', 'create', network])
        network_created = True
        run(['python3', str(root / 'scripts/bootstrap.py'), '--env-file', str(env_file)])
        stack = checkpoint.Stack(env_file)
        origin = 'localhost:' + port
        proof = recovery_assertions.ingest(stack, origin)
        objects = recovery_assertions.flush_s3(stack) if profile == 's3' else None
        output = run_checkpoint(root, 'backup', env_file)
        print(output, flush=True)
        source = next(line.removeprefix('Checkpoint: ') for line in output.splitlines() if line.startswith('Checkpoint: '))
        # A post-Checkpoint edit must disappear along with the disposable volumes.
        _, api, _ = recovery_assertions.client(env_file, origin)
        dashboard = dict(proof['dashboard'], title='post-Checkpoint edit')
        api('/api/dashboards/db', json.dumps(
            {'dashboard': dashboard, 'overwrite': True}).encode())
        started = time.monotonic()
        run(command + ['down', '-v', '--remove-orphans'])
        for key in checkpoint.bootstrap.VOLUMES:
            name = f'{project}_{key}'
            if checkpoint.bootstrap.run(['docker', 'volume', 'inspect', name]).returncode == 0:
                run(['docker', 'volume', 'rm', name])
        shutil.rmtree(stack.state / 'installation')
        # No marker producer survives the wipe, so queries cannot pass by reingestion.
        shutil.rmtree(stack.state / 'textfile')
        print(run_checkpoint(root, 'restore', env_file, source), flush=True)
        recovery_assertions.verify(env_file, origin, proof)
        if objects is not None:
            recovery_assertions.verify_objects(objects, recovery_assertions.s3_inventory(stack))
            print('ok: persisted Loki/Mimir S3 object bodies recovered unchanged', flush=True)
        print(f'RESTORE DRILL PASSED ({profile}): historical log, metric, trace and operator dashboard recovered; '
              f'RTO={time.monotonic() - started:.1f}s', flush=True)
        succeeded = True
    finally:
        cleanup_failed = cleanup(command, project, network, network_created, work, succeeded)
        if succeeded and cleanup_failed:
            raise RuntimeError('drill cleanup failed; inspect retained artifacts')



if __name__ == '__main__':
    def interrupted(signum, frame):
        raise RuntimeError('drill interrupted')
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGHUP, interrupted)
    try:
        main()
    except (RuntimeError, OSError, ValueError, KeyError, StopIteration, KeyboardInterrupt) as error:
        print(f'FAIL: {error}', file=sys.stderr)
        sys.exit(1)
