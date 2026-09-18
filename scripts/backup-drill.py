#!/usr/bin/env python3
"""Prove recovery using only a fresh disposable project."""
import os
import re
import shutil
import signal
import sys
import tempfile
import time
from pathlib import Path

import checkpoint
from smoke_assertions import ingest_marker, verify_marker


def main():
    root = checkpoint.ROOT
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
    env_file = work / '.env'
    settings = {
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
    try:
        run(['docker', 'network', 'create', network])
        network_created = True
        run(['python3', str(root / 'scripts/bootstrap.py'), '--env-file', str(env_file)])
        stack = checkpoint.Stack(env_file)
        proof = ingest_marker(env_file, 'localhost:' + port, stack.state)
        output = run([str(root / 'scripts/backup.sh'), '--env-file', str(env_file)])
        print(output, flush=True)
        source = next(line.removeprefix('Checkpoint: ') for line in output.splitlines() if line.startswith('Checkpoint: '))
        started = time.monotonic()
        run(command + ['down', '-v', '--remove-orphans'])
        for key in checkpoint.bootstrap.VOLUMES:
            name = f'{project}_{key}'
            if checkpoint.bootstrap.run(['docker', 'volume', 'inspect', name]).returncode == 0:
                run(['docker', 'volume', 'rm', name])
        shutil.rmtree(stack.state / 'installation')
        # No marker producer survives the wipe, so queries cannot pass by reingestion.
        shutil.rmtree(stack.state / 'textfile')
        print(run([str(root / 'scripts/restore.sh'), source, '--env-file', str(env_file)]), flush=True)
        verify_marker(env_file, 'localhost:' + port, proof)
        print(f'RESTORE DRILL PASSED: original log and metric recovered; RTO={time.monotonic() - started:.1f}s', flush=True)
        succeeded = True
    finally:
        run(command + ['down', '-v', '--remove-orphans'])
        for key in checkpoint.bootstrap.VOLUMES:
            name = f'{project}_{key}'
            if checkpoint.bootstrap.run(['docker', 'volume', 'inspect', name]).returncode == 0:
                run(['docker', 'volume', 'rm', name])
        if network_created:
            run(['docker', 'network', 'rm', network])
        if succeeded:
            shutil.rmtree(work)
        else:
            print(f'drill artifacts retained at {work}', file=sys.stderr)


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
