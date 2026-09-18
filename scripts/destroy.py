#!/usr/bin/env python3
"""Delete this installation's external volumes after typed project confirmation."""
import argparse
import fcntl
import json
import sys
from pathlib import Path

import bootstrap
from checkpoint import checked


def destroy(env_file, confirmation=input, runner=bootstrap.run):
    root = Path(__file__).resolve().parent.parent
    command = ['docker', 'compose', '--project-directory', str(root), '--env-file', str(env_file)]
    with env_file.with_name(env_file.name + '.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        config = json.loads(checked(command + ['--profile', 's3', 'config', '--format', 'json'], runner))
        project = config['name']
        names = [volume['name'] for volume in config['volumes'].values()]
        print('Volumes to delete: ' + ', '.join(names), file=sys.stderr)
        try:
            typed = confirmation(f'Type {project} to permanently delete these volumes: ')
        except EOFError:
            typed = ''
        if typed != project:
            raise RuntimeError('destroy refused: project name did not match')
        checked(command + ['--profile', 's3', 'down', '--remove-orphans'], runner)
        existing = checked(['docker', 'volume', 'ls', '--format', '{{.Name}}'], runner).split()
        for name in names:
            if name in existing:
                checked(['docker', 'volume', 'rm', name], runner)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file', type=Path, default=Path(__file__).resolve().parent.parent / '.env')
    try:
        destroy(parser.parse_args().env_file.resolve())
    except (RuntimeError, OSError, ValueError, KeyError) as error:
        print(f'FAIL: {error}', file=sys.stderr)
        sys.exit(1)
