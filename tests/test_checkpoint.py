"""Checkpoint secrecy, restore refusal and fence contract; no Docker calls."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
import checkpoint
import destroy
from types import SimpleNamespace


class CheckpointTests(unittest.TestCase):
    def test_manifest_has_keys_pins_checksums_and_no_env_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = root / '.env'
            env.write_text('OB_GRAFANA_ADMIN_PASSWORD=never-print-this-secret\nexport OB_S3_SECRET_KEY=another-secret\n')
            destination = root / 'checkpoint'
            destination.mkdir()
            (destination / 'grafana-data.tar').write_bytes(b'fixture')
            doc = checkpoint.manifest(destination, env, 'filesystem')
            self.assertEqual(doc['env_keys'], ['OB_GRAFANA_ADMIN_PASSWORD', 'OB_S3_SECRET_KEY'])
            self.assertNotIn('never-print-this-secret', json.dumps(doc))
            self.assertNotIn('another-secret', json.dumps(doc))
            self.assertTrue(all('@sha256:' in ref for ref in doc['images']))
            self.assertEqual(doc['artifacts']['grafana-data.tar']['size'], 7)
            self.assertEqual(len(doc['artifacts']['grafana-data.tar']['sha256']), 64)

    def test_restore_refuses_nonempty_volume_before_any_write(self):
        calls = []
        def runner(argv):
            calls.append(argv)
            output = 'restore-test_loki-data' if argv[:3] == ['docker', 'volume', 'ls'] else ''
            code = 1 if argv[:2] == ['docker', 'run'] else 0
            return subprocess.CompletedProcess(argv, code, output, '')
        with tempfile.TemporaryDirectory() as tmp:
            stack = object.__new__(checkpoint.Stack)
            stack.runner, stack.project, stack.state = runner, 'restore-test', Path(tmp)
            stack.image, stack.volumes = 'pinned-helper', ('loki-data',)
            stack.config = {'volumes': {'loki-data': {'name': 'restore-test_loki-data'}}}
            with patch.object(checkpoint, 'verify_checkpoint'), self.assertRaises(RuntimeError):
                checkpoint.restore(stack, Path(tmp))
            self.assertFalse(any(argv[:3] == ['docker', 'volume', 'create'] for argv in calls))
            self.assertTrue(any('type=volume,src=restore-test_loki-data,dst=/target,readonly' in argv for argv in calls))

    def test_fence_rejects_running_forced_oom_and_missing_containers(self):
        stack = object.__new__(checkpoint.Stack)
        stack.command = ['docker', 'compose']
        for status, code, oom, exists in [('exited', 0, False, True), ('running', 0, False, True),
                                         ('exited', 137, False, True), ('exited', 0, True, True),
                                         ('exited', 0, False, False)]:
            with self.subTest(status=status, code=code, oom=oom, exists=exists):
                def runner(argv):
                    output = json.dumps([{'State': {'Status': status, 'ExitCode': code, 'OOMKilled': oom}}])
                    if argv[:2] == ['docker', 'compose']:
                        output = 'container-id' if exists else ''
                    return subprocess.CompletedProcess(argv, 0, output, '')
                stack.runner = runner
                if status == 'exited' and code == 0 and not oom and exists:
                    stack.stopped_cleanly('loki')
                else:
                    with self.assertRaisesRegex(RuntimeError, 'fence failed'):
                        stack.stopped_cleanly('loki')

    def test_destroy_refuses_without_typed_project_name(self):
        calls = []
        def runner(argv):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, json.dumps({
                'name': 'observability-stack', 'volumes': {'loki-data': {'name': 'observability-stack_loki-data'}}}), '')
        with tempfile.TemporaryDirectory() as tmp:
            for typed in ('', 'wrong-project'):
                with self.assertRaisesRegex(RuntimeError, 'destroy refused'):
                    destroy.destroy(Path(tmp) / '.env', confirmation=lambda prompt: typed, runner=runner)
        self.assertTrue(all('config' in argv for argv in calls))

    def test_retention_keeps_newest_n_complete_sets(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            names = [f'20260917T02000000000{i}Z' for i in range(4)]
            for name in names:
                path = directory / name
                path.mkdir()
                (path / 'manifest.json').write_text('{}')
            incomplete = directory / '20260917T030000000000Z'
            incomplete.mkdir()
            unrelated = directory / 'operator-files'
            unrelated.mkdir()
            (unrelated / 'manifest.json').write_text('{}')
            checkpoint.prune_checkpoints(directory, 2)
            self.assertEqual([p.name for p in checkpoint.complete_checkpoints(directory)], names[-2:])
            self.assertTrue(incomplete.exists())
            self.assertTrue(unrelated.exists())

    def test_backup_refuses_low_free_space_before_fencing(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            last = directory / '20260917T020000000000Z'
            last.mkdir()
            (last / 'manifest.json').write_text('{}')
            (last / 'loki-data.tar').write_bytes(b'x' * 100)
            stack = SimpleNamespace(backups=directory, settings={})
            with patch.object(checkpoint.shutil, 'disk_usage', return_value=SimpleNamespace(free=101)):
                with self.assertRaisesRegex(RuntimeError, 'free space'):
                    checkpoint.backup(stack)
