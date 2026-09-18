"""Checkpoint secrecy, restore refusal and fence contract; no Docker calls."""
import contextlib
import io
import json
import shutil
import signal
import tarfile
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

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
                    checkpoint.backup(stack, sleep=lambda _: None)


class BackupAttestationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.stack = object.__new__(checkpoint.Stack)
        stack = self.stack
        stack.settings = {'OB_BACKUP_KEEP': '1'}
        stack.project, stack.mode = 'test', 'filesystem'
        stack.command = ['docker', 'compose']
        stack.writers, stack.volumes = ('loki',), ('loki-data',)
        stack.backups, stack.state = root / 'backups', root / 'state'
        stack.backups.mkdir()
        marker = stack.state / 'installation' / 'storage-mode'
        marker.parent.mkdir(parents=True)
        marker.write_text('filesystem')
        stack.env_file = root / '.env'
        stack.env_file.write_text('OB_GRAFANA_ADMIN_PASSWORD=secret')
        stack.config = {
            'services': {'loki': {'image': 'pinned', 'volumes': [
                {'type': 'volume', 'source': 'loki-data', 'target': '/loki'},
                {'type': 'bind', 'source': '/config', 'target': '/etc/loki', 'read_only': True}]}},
            'volumes': {'loki-data': {'name': 'test_loki-data'}}}
        self.container = {'Id': 'id', 'Config': {'Image': 'pinned', 'Labels': {
            'com.docker.compose.service': 'loki', 'com.docker.compose.project': 'test'}},
            'Mounts': [{'Type': 'volume', 'Name': 'test_loki-data', 'Destination': '/loki', 'RW': True},
                       {'Type': 'bind', 'Source': '/config', 'Destination': '/etc/loki', 'RW': False}]}
        self.calls, self.stopped = [], False
        self.missing_volume, self.foreign_consumer = False, False
        self.restart_error = False
        def runner(argv):
            self.calls.append(argv)
            output, code = '', 0
            if argv[:3] == ['docker', 'volume', 'inspect']:
                code = int(self.missing_volume)
                output = json.dumps([{'Name': 'test_loki-data'}])
            elif argv[:2] == ['docker', 'inspect']:
                self.container['State'] = {'Status': 'exited' if self.stopped else 'running',
                                           'ExitCode': 0, 'OOMKilled': False}
                output = json.dumps([self.container])
            elif argv[:2] == ['docker', 'ps']:
                output = 'foreign' if self.foreign_consumer else ('' if self.stopped else 'id')
            elif argv[:2] == ['docker', 'compose']:
                if argv[2] == 'ps':
                    output = 'loki' if '--services' in argv else 'id'
                elif argv[2] == 'stop':
                    self.stopped = True
                elif argv[2] == 'start':
                    self.stopped = False
                    code = int(self.restart_error)
            return subprocess.CompletedProcess(argv, code, output, '')
        stack.runner = runner
        stack.helper = Mock(side_effect=self.archive)
        stack.wait_ready = Mock()
        self.files = patch.object(checkpoint, 'configuration_files', return_value=[])
        self.files.start()
        self.addCleanup(self.files.stop)

    def archive(self, mounts, script, key, *args):
        destination = next(m.split('src=')[1].split(',')[0] for m in mounts if 'dst=/checkpoint' in m)
        with tarfile.open(Path(destination) / (key + '.tar'), 'w') as archive:
            entry = tarfile.TarInfo('data')
            entry.size = 7
            archive.addfile(entry, io.BytesIO(b'archive'))

    def assert_refused_before_capture(self, message):
        with self.assertRaisesRegex(RuntimeError, message):
            checkpoint.backup(self.stack, sleep=lambda _: None)
        self.stack.helper.assert_not_called()
        self.assertFalse(any('stop' in argv for argv in self.calls))
        self.assertEqual(list(self.stack.backups.iterdir()), [])

    def test_changed_live_mounts_or_pins_refused_before_capture(self):
        import copy
        original = copy.deepcopy(self.container)
        for change in ('prefix', 'bind', 'readonly', 'extra', 'image'):
            with self.subTest(change=change):
                self.container = copy.deepcopy(original)
                if change == 'prefix':
                    self.container['Mounts'][0]['Name'] = 'old_loki-data'
                elif change == 'bind':
                    self.container['Mounts'][1]['Source'] = '/other-config'
                elif change == 'readonly':
                    self.container['Mounts'][0]['RW'] = False
                elif change == 'extra':
                    self.container['Mounts'].append({'Type': 'volume', 'Name': 'unarchived',
                                                     'Destination': '/extra', 'RW': True})
                else:
                    self.container['Config']['Image'] = 'old-pin'
                self.assert_refused_before_capture('mount|pins')

    def test_absent_source_volume_never_created_by_archive_helper(self):
        self.missing_volume = True
        self.assert_refused_before_capture('failed|volume')

    def test_foreign_consumer_refused_before_capture_and_rechecked_under_fence(self):
        self.foreign_consumer = True
        self.assert_refused_before_capture('consumer')
        self.foreign_consumer = False
        original = self.stack.stopped_cleanly
        def stopped(service):
            original(service)
            self.foreign_consumer = True
        self.stack.stopped_cleanly = stopped
        with self.assertRaisesRegex(RuntimeError, 'consumer'):
            checkpoint.backup(self.stack, sleep=lambda _: None)
        self.stack.helper.assert_not_called()
        self.assertTrue(any('start' in argv for argv in self.calls))

    def test_resume_or_readiness_failure_preserves_success_timestamp_and_retention(self):
        textfile = self.stack.state / 'textfile'
        textfile.mkdir()
        metric = textfile / 'checkpoint.prom'
        metric.write_text('previous-success')
        old = self.stack.backups / '20260917T020000000000Z'
        old.mkdir()
        (old / 'manifest.json').write_text('{}')
        for failure in ('start', 'readiness'):
            with self.subTest(failure=failure):
                self.restart_error = failure == 'start'
                self.stack.wait_ready.side_effect = RuntimeError('not healthy') if failure == 'readiness' else None
                with self.assertRaises(RuntimeError):
                    checkpoint.backup(self.stack, sleep=lambda _: None)
                self.assertEqual(metric.read_text(), 'previous-success')
                self.assertTrue(old.exists())
                self.assertGreater(len(checkpoint.complete_checkpoints(self.stack.backups)), 1)

    def test_success_and_pruning_only_after_healthy_resume(self):
        old = self.stack.backups / '20260917T020000000000Z'
        old.mkdir()
        (old / 'manifest.json').write_text('{}')
        def ready():
            self.assertTrue(old.exists())
            self.assertFalse((self.stack.state / 'textfile/checkpoint.prom').exists())
            self.assertFalse(self.stopped)
        self.stack.wait_ready.side_effect = ready
        destination = checkpoint.backup(self.stack, sleep=lambda _: None)
        self.stack.wait_ready.assert_called_once()
        self.assertTrue((destination / 'manifest.json').exists())
        self.assertFalse(old.exists())
        self.assertIn('stack_checkpoint_last_success', (self.stack.state / 'textfile/checkpoint.prom').read_text())


    def test_capture_rejects_unrestorable_tar_before_manifest_or_pruning(self):
        old = self.stack.backups / '20260917T020000000000Z'
        old.mkdir()
        (old / 'manifest.json').write_text('{}')
        def archive(mounts, script, key, *args):
            destination = next(m.split('src=')[1].split(',')[0] for m in mounts if 'dst=/checkpoint' in m)
            with tarfile.open(Path(destination) / (key + '.tar'), 'w') as handle:
                member = tarfile.TarInfo('unsafe-link')
                member.type, member.linkname = tarfile.SYMTYPE, '/outside'
                handle.addfile(member)
        self.stack.helper.side_effect = archive
        with self.assertRaisesRegex(RuntimeError, 'unsupported archive member'):
            checkpoint.backup(self.stack, sleep=lambda _: None)
        self.assertEqual(checkpoint.complete_checkpoints(self.stack.backups), [old])
        self.assertFalse((self.stack.state / 'textfile/checkpoint.prom').exists())
        self.assertFalse(self.stopped)

    def test_capture_error_survives_failed_resume_and_repeated_signals(self):
        signals = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)
        def interrupted(signum, frame):
            raise RuntimeError('repeated interruption during resume')
        for sig in signals:
            previous = signal.signal(sig, interrupted)
            self.addCleanup(signal.signal, sig, previous)
        resumed = False
        self.stack.helper.side_effect = RuntimeError('capture failed first')
        runner = self.stack.runner
        def resume(argv):
            nonlocal resumed
            if argv[:3] == ['docker', 'compose', 'start']:
                for sig in signals:
                    signal.raise_signal(sig)
                resumed = True
                raise checkpoint.bootstrap.Refused('not_ready', 'private upstream detail')
            return runner(argv)
        self.stack.runner = resume
        with self.assertRaisesRegex(RuntimeError, 'capture failed first'), contextlib.redirect_stderr(io.StringIO()):
            checkpoint.backup(self.stack, sleep=lambda _: None)
        self.assertTrue(resumed)
        self.assertEqual({signal.getsignal(sig) for sig in signals}, {interrupted})
        self.assertFalse(checkpoint.complete_checkpoints(self.stack.backups))
        self.assertFalse((self.stack.state / 'textfile/checkpoint.prom').exists())


class CheckpointVerificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.checkout = self.root / 'checkout'
        self.checkout.mkdir()
        (self.checkout / 'docker').mkdir()
        for name in ('compose.yaml', 'compose.s3.yaml', 'config.alloy'):
            shutil.copy2(checkpoint.ROOT / name, self.checkout / name)
        root_patch = patch.object(checkpoint, 'ROOT', self.checkout)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        self.env = self.root / '.env'
        self.env.write_text('OB_GRAFANA_ADMIN_PASSWORD=original-secret\nOB_S3_ACCESS_KEY=access\n'
                            'OB_S3_SECRET_KEY=secret\nOB_CAPTURED_OPTIONAL=private-value\n')
        self.source = self.root / 'checkpoint'
        self.source.mkdir()
        (self.source / 'installation').mkdir()
        (self.source / 'installation/storage-mode').write_text('filesystem')
        (self.source / 'configuration').mkdir()
        for path in checkpoint.configuration_files():
            shutil.copy2(path, self.source / 'configuration' / path.name)
        with tarfile.open(self.source / 'loki-data.tar', 'w') as archive:
            archive.addfile(tarfile.TarInfo('empty-file'))
        self.stack = object.__new__(checkpoint.Stack)
        self.stack.env_file, self.stack.mode = self.env, 'filesystem'
        self.stack.volumes, self.stack.settings = ('loki-data',), {'OB_ALERTS': 'placeholder'}
        self.stack.project, self.stack.state = 'recovery-test', self.root / 'state'
        self.stack.image = 'helper'
        self.stack.config = {'volumes': {'loki-data': {'name': 'recovery-test_loki-data'}}}
        self.publish(self.source)

    def publish(self, source):
        (source / 'manifest.json').write_text(json.dumps(checkpoint.manifest(source, self.env, 'filesystem')))

    def test_verify_checkpoint_rejects_corruption_extra_files_symlinks_pins_and_config(self):
        checkpoint.verify_checkpoint(self.stack, self.source)
        for defect in ('hash', 'extra', 'symlink', 'pins', 'config'):
            with self.subTest(defect=defect):
                source = self.root / defect
                shutil.copytree(self.source, source)
                if defect == 'hash':
                    (source / 'loki-data.tar').write_bytes(b'corrupt')
                elif defect == 'extra':
                    (source / 'extra').write_text('unexpected')
                elif defect == 'symlink':
                    (source / 'extra').symlink_to(self.env)
                elif defect == 'pins':
                    doc = json.loads((source / 'manifest.json').read_text())
                    doc['images'] = ['different-pin']
                    (source / 'manifest.json').write_text(json.dumps(doc))
                else:
                    (source / 'configuration/config.alloy').write_text('different config')
                    self.publish(source)
                with self.assertRaises(RuntimeError):
                    checkpoint.verify_checkpoint(self.stack, source)
        self.env.write_text(self.env.read_text().replace('OB_CAPTURED_OPTIONAL=private-value\n', ''))
        with contextlib.redirect_stderr(io.StringIO()) as diagnostic:
            checkpoint.verify_checkpoint(self.stack, self.source)
        self.assertIn('OB_CAPTURED_OPTIONAL', diagnostic.getvalue())
        self.assertNotIn('original-secret', diagnostic.getvalue())
        self.assertNotIn('private-value', diagnostic.getvalue())

    def test_verify_checkpoint_rejects_traversal_absolute_paths_and_tar_links(self):
        for name, kind in (('../escape', tarfile.REGTYPE), ('/escape', tarfile.REGTYPE),
                           ('link', tarfile.SYMTYPE), ('hardlink', tarfile.LNKTYPE)):
            with self.subTest(name=name):
                with tarfile.open(self.source / 'loki-data.tar', 'w') as archive:
                    member = tarfile.TarInfo(name)
                    member.type, member.linkname = kind, 'empty-file'
                    archive.addfile(member)
                self.publish(self.source)
                with self.assertRaisesRegex(RuntimeError, 'unsupported archive member'):
                    checkpoint.verify_checkpoint(self.stack, self.source)

    def test_restore_refuses_foreign_consumers_alert_errors_and_missing_network_before_writes(self):
        volume = self.root / 'target-volume'
        volume.mkdir()
        def runner(argv):
            if argv[:3] == ['docker', 'volume', 'ls']:
                output = 'recovery-test_loki-data'
            elif argv[:2] == ['docker', 'ps']:
                output = 'foreign-container' if 'volume=recovery-test_loki-data' in argv and scenario == 'consumer' else ''
            elif argv[:3] == ['docker', 'network', 'inspect'] or argv[:3] == ['docker', 'network', 'create']:
                return subprocess.CompletedProcess(argv, 1, '', 'private network diagnostics')
            else:
                (volume / 'unexpected-write').touch()
                output = ''
            return subprocess.CompletedProcess(argv, 0, output, '')
        def helper(mounts, script, *args):
            if 'tar -xpf' in script:
                (volume / 'unexpected-extraction').touch()
        self.stack.runner, self.stack.helper = runner, helper
        for scenario in ('consumer', 'alert', 'smtp', 'network'):
            with self.subTest(scenario=scenario):
                self.stack.settings = {} if scenario == 'alert' else {'OB_ALERTS': 'placeholder'}
                if scenario == 'smtp':
                    self.stack.settings = {'OB_ALERT_EMAIL': 'ops@example.com',
                                           'OB_SMTP_URL': 'smtp://user:bad%0Apassword@example.com'}
                with self.assertRaises((RuntimeError, checkpoint.bootstrap.Refused)):
                    checkpoint.restore(self.stack, self.source)
                self.assertEqual(list(volume.iterdir()), [])
                self.assertFalse(self.stack.state.exists())
