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

PIN = 'example/loki@sha256:' + 'a' * 64
IMAGE_ID = 'sha256:' + 'b' * 64


def image_result(argv):
    return subprocess.CompletedProcess(argv, 0, json.dumps([{'Id': IMAGE_ID, 'RepoDigests': [PIN]}]), '')


class CheckpointTests(unittest.TestCase):
    def test_image_preflight_names_the_service_and_remedy_without_diagnostics(self):
        stack = object.__new__(checkpoint.Stack)
        stack.config = {'services': {'loki': {'image': 'private.example/loki:trial'}}}
        for failure in ('configured', 'digest'):
            with self.subTest(failure=failure):
                def runner(argv):
                    if failure == 'configured' or '@' in argv[-1]:
                        return subprocess.CompletedProcess(argv, 1, '', 'private-credential')
                    return image_result(argv)
                stack.runner = runner
                with self.assertRaisesRegex(RuntimeError, 'loki: image unavailable locally; pull or load') as error:
                    stack.resolve_images()
                self.assertNotIn('private', str(error.exception))

    def test_capture_prefers_the_configured_registry_with_a_port(self):
        stack = object.__new__(checkpoint.Stack)
        reference = 'registry.example:5000/store:trial'
        expected = 'registry.example:5000/store@sha256:' + 'a' * 64
        stack.config = {'services': {'loki': {'image': reference}}}
        stack.runner = lambda argv: subprocess.CompletedProcess(argv, 0, json.dumps([
            {'Id': IMAGE_ID, 'RepoDigests': [PIN, expected]}]), '')
        stack.resolve_images()
        self.assertEqual(stack.images, {'loki': expected})

    def test_manifest_has_keys_pins_checksums_and_no_env_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = root / '.env'
            env.write_text('OB_GRAFANA_ADMIN_PASSWORD=never-print-this-secret\nexport OB_S3_SECRET_KEY=another-secret\n')
            destination = root / 'checkpoint'
            destination.mkdir()
            (destination / 'grafana-data.tar').write_bytes(b'fixture')
            stack = object.__new__(checkpoint.Stack)
            stack.config = {'services': {'caddy': {'image': 'local:experiment'},
                                         'loki': {'image': 'registry/loki:experiment'}}}
            stack.runner = image_result
            stack.resolve_images()
            self.assertEqual(stack.image, IMAGE_ID)
            calls = []
            stack.command = ['docker', 'compose']
            stack.runner = lambda argv: (calls.append(argv) or subprocess.CompletedProcess(argv, 0, '', ''))
            stack.dc('up', '-d')
            self.assertIn('OB_LOKI_IMAGE=' + PIN, calls[0])
            doc = checkpoint.manifest(destination, env, 'filesystem', stack.images)
            self.assertEqual(doc['env_keys'], ['OB_GRAFANA_ADMIN_PASSWORD', 'OB_S3_SECRET_KEY'])
            self.assertNotIn('never-print-this-secret', json.dumps(doc))
            self.assertNotIn('another-secret', json.dumps(doc))
            self.assertTrue(all('@sha256:' in ref for ref in doc['images'].values()))
            self.assertEqual(doc['images'], {'caddy': PIN, 'loki': PIN})
            self.assertEqual(doc['artifacts']['grafana-data.tar']['size'], 7)
            self.assertEqual(len(doc['artifacts']['grafana-data.tar']['sha256']), 64)

    def test_restore_refuses_nonempty_volume_before_any_write(self):
        calls = []
        def runner(argv):
            calls.append(argv)
            output = 'restore-test_loki-data' if argv[:3] == ['docker', 'volume', 'ls'] else ''
            code = 1 if argv[:2] == ['docker', 'create'] else 0
            if argv[:2] == ['docker', 'inspect']:
                return subprocess.CompletedProcess(argv, 1, '', '')
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
        self.container = {'Id': 'id', 'Image': IMAGE_ID, 'Config': {'Image': 'pinned', 'Labels': {
            'com.docker.compose.service': 'loki', 'com.docker.compose.project': 'test'}},
            'Mounts': [{'Type': 'volume', 'Name': 'test_loki-data', 'Destination': '/loki', 'RW': True},
                       {'Type': 'bind', 'Source': '/config', 'Destination': '/etc/loki', 'RW': False}]}
        self.calls, self.stopped = [], False
        self.missing_volume, self.foreign_consumer = False, False
        self.restart_error = False
        self.repo_digests = [PIN]
        self.digest_id = IMAGE_ID
        def runner(argv):
            self.calls.append(argv)
            if argv[0] == 'env':
                argv = argv[argv.index('docker'):]
            if argv[:3] == ['docker', 'image', 'inspect']:
                return subprocess.CompletedProcess(argv, 0, json.dumps([
                    {'Id': self.digest_id if '@' in argv[-1] else IMAGE_ID, 'RepoDigests': self.repo_digests}]), '')
            output, code = '', 0
            if argv[:3] == ['docker', 'volume', 'inspect']:
                code = int(self.missing_volume)
                output = json.dumps([{'Name': 'test_loki-data'}])
            elif argv[:2] == ['docker', 'inspect']:
                self.container['State'] = {'Status': 'exited' if self.stopped else 'running',
                                           'Running': not self.stopped, 'ExitCode': 0, 'OOMKilled': False}
                output = json.dumps([self.container])
            elif argv[:2] == ['docker', 'ps']:
                output = 'foreign' if self.foreign_consumer else ('' if self.stopped else 'id')
            elif argv[:2] == ['docker', 'compose']:
                if argv[2] == 'ps':
                    output = 'loki' if '--services' in argv else 'id'
                elif argv[2] == 'stop':
                    self.stopped = True
                elif argv[2] == 'start':
                    code = int(self.restart_error)
                    if not code:
                        self.stopped = False
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
        for change in ('prefix', 'bind', 'readonly', 'extra', 'image', 'local-only', 'unverified'):
            with self.subTest(change=change):
                self.container = copy.deepcopy(original)
                self.repo_digests = [PIN]
                self.digest_id = IMAGE_ID
                if change == 'prefix':
                    self.container['Mounts'][0]['Name'] = 'old_loki-data'
                elif change == 'bind':
                    self.container['Mounts'][1]['Source'] = '/other-config'
                elif change == 'readonly':
                    self.container['Mounts'][0]['RW'] = False
                elif change == 'extra':
                    self.container['Mounts'].append({'Type': 'volume', 'Name': 'unarchived',
                                                     'Destination': '/extra', 'RW': True})
                elif change == 'image':
                    self.container['Image'] = 'sha256:' + 'c' * 64
                elif change == 'unverified':
                    self.digest_id = 'sha256:' + 'd' * 64
                else:
                    self.repo_digests = []
                self.assert_refused_before_capture('mount|identity|RepoDigests')

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
                if failure == 'start':
                    self.stack.wait_ready = checkpoint.Stack.wait_ready.__get__(self.stack)
                else:
                    self.stack.wait_ready = Mock(side_effect=RuntimeError('not healthy'))
                with self.assertRaises(RuntimeError), contextlib.redirect_stderr(io.StringIO()) as diagnostic, \
                        patch.object(checkpoint.time, 'monotonic', side_effect=range(0, 1000, 10)), \
                        patch.object(checkpoint.time, 'sleep'):
                    checkpoint.backup(self.stack, sleep=lambda _: None)
                if failure == 'start':
                    self.assertGreater(sum(argv[:3] == ['docker', 'compose', 'start'] for argv in self.calls), 1)
                self.assertIn('Checkpoint complete but services not resumed:', diagnostic.getvalue())
                self.assertEqual(metric.read_text(), 'previous-success')
                self.assertTrue(old.exists())
                self.assertGreater(len(checkpoint.complete_checkpoints(self.stack.backups)), 1)

    def test_success_and_pruning_only_after_healthy_resume(self):
        old = self.stack.backups / '20260917T020000000000Z'
        old.mkdir()
        (old / 'manifest.json').write_text('{}')
        def ready(**kwargs):
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

    def tempo_fence(self, metrics):
        stack = self.stack
        stack.writers = checkpoint.WRITERS + ('rustfs',)
        stack.images = {'loki': PIN}
        stack.attest_capture = lambda: None
        stack.check_capture_volumes = lambda: None
        stack.image = 'pinned-helper'
        events, helpers = [], {}
        clock = [0]
        original = stack.runner
        def runner(argv):
            output = ''
            if argv[:3] == ['docker', 'compose', 'ps'] and '--services' in argv:
                output = ' '.join(stack.writers)
            elif argv[:4] == ['docker', 'compose', 'ps', '-q'] and argv[-1] == 'tempo':
                output = 'tempo-id'
            elif argv[:2] == ['docker', 'inspect'] and argv[-1] == 'tempo-id':
                output = json.dumps([{'Id': 'tempo-id', 'State': {'Running': True}, 'Config': {'Labels': {
                    'com.docker.compose.project': 'test', 'com.docker.compose.service': 'tempo'}}}])
            elif argv[:2] == ['docker', 'create']:
                self.assertEqual(argv[argv.index('--network') + 1], 'container:tempo-id')
                name = argv[argv.index('--name') + 1]
                label = argv[argv.index('--label') + 1]
                helpers[name] = {'Id': name, 'Config': {'Labels': dict([label.split('=', 1)])}}
                output = name
            elif argv[:3] == ['docker', 'start', '-a']:
                events.append(('probe', clock[0]))
                output = metrics(clock[0])
            elif argv[:2] == ['docker', 'inspect'] and argv[-1] in helpers:
                output = json.dumps([helpers[argv[-1]]])
            elif argv[:3] == ['docker', 'rm', '-f']:
                del helpers[argv[-1]]
            else:
                if argv[:3] == ['docker', 'compose', 'stop']:
                    events.append((argv[-1], clock[0]))
                return original(argv)
            return subprocess.CompletedProcess(argv, 0, output, '')
        def helper(mounts, script, *args, **kwargs):
            if 'network' in kwargs:
                self.assertGreater(kwargs['timeout'], 0)
                return checkpoint.Stack.helper(stack, mounts, script, *args, **kwargs)
            return self.archive(mounts, script, *args)
        stack.runner = runner
        stack.helper.side_effect = helper
        def sleep(seconds):
            clock[0] += seconds
        return events, helpers, clock, sleep

    def test_tempo_fence_waits_after_query_sources_and_mimir_before_clean_stop(self):
        events, helpers, clock, sleep = self.tempo_fence(
            lambda now: 'tempo_query_frontend_connected_clients 2\n')
        with patch.object(checkpoint.time, 'monotonic', side_effect=lambda: clock[0]):
            destination = checkpoint.backup(self.stack, timeout=1, sleep=sleep)
        self.assertEqual([name for name, _ in events if name != 'probe'], list(self.stack.writers))
        self.assertEqual(events[5], ('probe', 0))
        self.assertEqual(events[-2:], [('tempo', 35), ('rustfs', 35)])
        self.assertEqual(helpers, {})
        self.assertTrue((destination / 'manifest.json').exists())

    def test_tempo_quiescence_busy_and_failed_probes_restart_quiet_window(self):
        def metrics(now):
            if now == 25:
                raise RuntimeError('transient fetch failure')
            if now == 35:
                return 'unrelated_metric 1\n'
            if now == 45:
                return 'tempo_query_frontend_connected_clients 0\n'
            queued = 1 if now < 5 or now == 15 else 0
            return ('tempo_query_frontend_connected_clients 2\n'
                    'tempo_query_frontend_queue_length{user="idle"} 0\n'
                    f'tempo_query_frontend_queue_length{{user="busy"}} {queued}\n')
        events, helpers, clock, sleep = self.tempo_fence(metrics)
        with patch.object(checkpoint.time, 'monotonic', side_effect=lambda: clock[0]):
            destination = checkpoint.backup(self.stack, sleep=sleep)
        self.assertEqual(events[-2:], [('tempo', 81), ('rustfs', 81)])
        self.assertEqual(helpers, {})
        self.assertTrue((destination / 'manifest.json').exists())

    def test_tempo_quiescence_deadline_resumes_without_stopping_tempo_or_capturing(self):
        def metrics(now):
            if now >= 10:
                raise subprocess.TimeoutExpired(['docker', 'start'], 5)
            return ('tempo_query_frontend_connected_clients 2\n'
                    'tempo_query_frontend_queue_length{user="busy"} 1\n')
        events, helpers, clock, sleep = self.tempo_fence(metrics)
        runner = self.stack.runner
        def ownership(argv):
            result = runner(argv)
            if argv[:2] == ['docker', 'inspect'] and argv[-1] == 'tempo-id' and clock[0] < 5:
                containers = json.loads(result.stdout)
                containers[0]['Config']['Labels']['com.docker.compose.project'] = 'foreign'
                result.stdout = json.dumps(containers)
            return result
        self.stack.runner = ownership
        old = self.stack.backups / '20260917T020000000000Z'
        old.mkdir()
        (old / 'manifest.json').write_text('{}')
        metric = self.stack.state / 'textfile/checkpoint.prom'
        metric.parent.mkdir()
        metric.write_text('previous-success')
        with patch.object(checkpoint.time, 'monotonic', side_effect=lambda: clock[0]), \
                self.assertRaisesRegex(RuntimeError, 'Tempo.*quiescence.*deadline'):
            checkpoint.backup(self.stack, timeout=40, sleep=sleep)
        self.assertEqual(clock[0], 120)
        self.assertEqual(next(event for event in events if event[0] == 'probe'), ('probe', 5))
        self.assertNotIn('tempo', [name for name, _ in events])
        self.assertNotIn('rustfs', [name for name, _ in events])
        self.assertEqual(helpers, {})
        self.assertEqual(checkpoint.complete_checkpoints(self.stack.backups), [old])
        self.assertEqual(metric.read_text(), 'previous-success')
        self.assertFalse(self.stopped)

    def test_signal_during_failed_probe_create_aborts_after_owned_cleanup(self):
        events, helpers, clock, sleep = self.tempo_fence(
            lambda now: 'tempo_query_frontend_connected_clients 2\n')
        runner = self.stack.runner
        creates = []
        signal_in_create = True
        def fail_create(argv):
            result = runner(argv)
            if argv[:2] == ['docker', 'create']:
                creates.append(result.stdout)
                if signal_in_create:
                    signal.raise_signal(signal.SIGTERM)
                result.returncode = 1
            return result
        self.stack.runner = fail_create
        with patch.object(checkpoint.time, 'monotonic', side_effect=lambda: clock[0]), \
                self.assertRaises(checkpoint.Interrupted) as raised:
            checkpoint.backup(self.stack, sleep=sleep)
        self.assertEqual(len(creates), 1)
        self.assertEqual(helpers, {})
        self.assertIn('operation failed', str(raised.exception.__cause__))
        self.assertFalse(any(name in ('probe', 'tempo', 'rustfs') for name, _ in events))
        self.assertFalse(checkpoint.complete_checkpoints(self.stack.backups))
        self.assertFalse(self.stopped)
        # A signal delivered inside the retry handler must still cancel the capture.
        signal_in_create = False
        def trace(frame, event, arg):
            if (event == 'line' and frame.f_code.co_name == 'quiesce_tempo'
                    and isinstance(sys.exc_info()[1], RuntimeError)):
                signal.raise_signal(signal.SIGTERM)
            return trace
        with patch.object(checkpoint.time, 'monotonic', side_effect=lambda: clock[0]), \
                self.assertRaises(checkpoint.Interrupted):
            sys.settrace(trace)
            try:
                checkpoint.backup(self.stack, sleep=sleep)
            finally:
                sys.settrace(None)
        self.assertEqual(len(creates), 2)
        self.assertEqual(helpers, {})
        self.assertFalse(checkpoint.complete_checkpoints(self.stack.backups))
        self.assertFalse(self.stopped)

    def test_probe_cleanup_failure_aborts_without_creating_more_helpers(self):
        def metrics(now):
            raise RuntimeError('probe failed first')
        events, helpers, clock, sleep = self.tempo_fence(metrics)
        runner = self.stack.runner
        def fail_cleanup(argv):
            if argv[:3] == ['docker', 'rm', '-f']:
                return subprocess.CompletedProcess(argv, 1, '', 'private daemon diagnostic')
            return runner(argv)
        self.stack.runner = fail_cleanup
        with patch.object(checkpoint.time, 'monotonic', side_effect=lambda: clock[0]), \
                contextlib.redirect_stderr(io.StringIO()) as diagnostic, \
                self.assertRaisesRegex(checkpoint.CleanupFailed, 'probe failed first') as raised:
            checkpoint.backup(self.stack, sleep=sleep)
        self.assertEqual(str(raised.exception.__cause__), 'probe failed first')
        self.assertEqual(len(helpers), 1)
        self.assertEqual([event for event in events if event[0] == 'probe'], [('probe', 0)])
        self.assertFalse(any(name in ('tempo', 'rustfs') for name, _ in events))
        self.assertIn('cleanup', diagnostic.getvalue())
        self.assertNotIn('private daemon diagnostic', diagnostic.getvalue())
        self.assertFalse(checkpoint.complete_checkpoints(self.stack.backups))
        self.assertFalse(self.stopped)

    def test_readiness_propagates_interrupted_without_retrying(self):
        interrupted = checkpoint.Interrupted('operator cancelled readiness')
        del self.stack.wait_ready
        with patch.object(checkpoint.bootstrap, 'wait_ready', side_effect=interrupted) as probe, \
                patch.object(checkpoint.time, 'sleep') as sleep, \
                patch.object(checkpoint.time, 'monotonic', side_effect=range(10)), \
                self.assertRaises(checkpoint.Interrupted) as raised:
            self.stack.wait_ready(timeout=3)
        self.assertIs(raised.exception, interrupted)
        self.assertEqual(probe.call_count, 1)
        sleep.assert_not_called()

    def test_slow_initial_start_consumes_settle_without_exhausting_poll_budget(self):
        now = 0
        def start(*args):
            nonlocal now
            now = 150
        with patch.object(self.stack, 'dc', side_effect=start), \
                patch.object(self.stack, 'wait_ready') as ready, \
                patch.object(checkpoint.time, 'monotonic', side_effect=lambda: now):
            self.stack.resume(['caddy'], timeout=120, settle=130)
        ready.assert_called_once_with(restart=True, timeout=100, settle=0)

    def test_invalid_stop_timeout_rejected_before_env_lock_or_docker(self):
        for timeout in (0, -1):
            with self.subTest(timeout=timeout):
                with self.assertRaisesRegex(RuntimeError, 'positive'):
                    checkpoint.backup(self.stack, timeout=timeout, sleep=lambda _: None)
                with patch.object(sys, 'argv', ['checkpoint.py', 'backup', '--stop-timeout', str(timeout),
                                               '--env-file', str(self.stack.env_file)]), \
                        contextlib.redirect_stderr(io.StringIO()) as diagnostic, \
                        self.assertRaises(SystemExit) as raised:
                    checkpoint.main()
                self.assertEqual(raised.exception.code, 2)
                self.assertIn('positive', diagnostic.getvalue())
                self.assertFalse(self.stack.env_file.with_name(self.stack.env_file.name + '.lock').exists())
        self.assertEqual(self.calls, [])
        self.assertEqual(list(self.stack.backups.iterdir()), [])

    def test_resume_recovers_late_stop_missing_writer_and_waits_for_rustfs_health(self):
        stack = self.stack
        stack.writers = ('loki', 'rustfs')
        stack.config['services']['rustfs'] = {'healthcheck': {'test': ['CMD', 'probe']}}
        del stack.wait_ready
        clock = [0]
        state = {'loki': 'missing', 'rustfs': 'missing-health'}
        recover_health = True
        started = stack.state / 'restarted-after-late-stop'
        created = stack.state / 'recreated-missing-writer'
        attempts = []
        first_ps = True
        permanent = False
        def runner(argv):
            nonlocal first_ps
            attempts.append(argv)
            output, code = '', 0
            if argv[:3] == ['docker', 'compose', 'ps']:
                if first_ps or permanent:
                    first_ps = False
                    return subprocess.CompletedProcess(argv, 1, '', 'private daemon error')
                output = '' if state[argv[-1]] == 'missing' else argv[-1]
            elif argv[:3] == ['docker', 'compose', 'up']:
                created.touch()
                state[argv[-1]] = 'stopped'
            elif argv[:3] == ['docker', 'compose', 'start']:
                if len(argv) > 4:
                    code = 1  # The original stop is still finishing on the daemon.
                else:
                    state[argv[-1]] = 'running'
                    started.touch()
            elif argv[:2] == ['docker', 'inspect']:
                service = argv[-1]
                container_state = {'Running': state[service] not in ('missing', 'stopped')}
                if service == 'rustfs' and state[service] != 'missing-health':
                    container_state['Health'] = {'Status': state[service]}
                output = json.dumps([{'State': container_state}])
            return subprocess.CompletedProcess(argv, code, output, '')
        def sleep(seconds):
            clock[0] += seconds
            if started.exists() and recover_health:
                state['rustfs'] = 'healthy'
        def probe(*args, **kwargs):
            self.assertTrue(created.exists())
            self.assertTrue(started.exists())
            self.assertEqual(state['rustfs'], 'healthy')
        stack.runner = runner
        with patch.object(checkpoint.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(checkpoint.time, 'sleep', side_effect=sleep), \
                patch.object(checkpoint.bootstrap, 'wait_ready', side_effect=probe) as http:
            stack.resume(stack.writers, timeout=8, settle=2)
            self.assertEqual(http.call_count, 5)
            recover_health = False
            state['rustfs'] = 'unhealthy'
            with self.assertRaisesRegex(RuntimeError, 'deadline'):
                stack.wait_ready(restart=True, timeout=3)
            self.assertEqual(http.call_count, 5)
            permanent = True
            before = len(attempts)
            with self.assertRaisesRegex(RuntimeError, 'deadline'):
                stack.wait_ready(restart=True, timeout=3)
            self.assertGreaterEqual(len(attempts) - before, 2)

    def test_helper_failure_removes_only_its_owned_container_before_resume(self):
        stack = self.stack
        stack.image = 'pinned-helper'
        del stack.helper
        containers = {'test-checkpoint-helper': {'Id': 'foreign', 'Config': {'Labels': {}}}}
        names = []
        original = stack.runner
        removed = stack.state / 'owned-helper-removed'
        def runner(argv):
            if argv[:2] == ['docker', 'create']:
                name = argv[argv.index('--name') + 1]
                label = argv[argv.index('--label') + 1]
                names.append(name)
                containers[name] = {'Id': 'owned', 'Config': {'Labels': dict([label.split('=', 1)])}}
                return subprocess.CompletedProcess(argv, 0, 'owned', '')
            if argv[:3] == ['docker', 'start', '-a']:
                (stack.state / 'partial-archive').write_text('partial')
                raise RuntimeError('archive command interrupted')
            if argv[:2] == ['docker', 'inspect'] and argv[-1] in containers:
                return subprocess.CompletedProcess(argv, 0, json.dumps([containers[argv[-1]]]), '')
            if argv[:3] == ['docker', 'rm', '-f']:
                self.assertEqual(argv[-1], 'owned')
                del containers[names[-1]]
                removed.touch()
                return subprocess.CompletedProcess(argv, 0, '', '')
            if argv[:3] == ['docker', 'compose', 'start']:
                self.assertTrue(removed.exists())
            return original(argv)
        stack.runner = runner
        with self.assertRaisesRegex(RuntimeError, 'archive command interrupted'):
            checkpoint.backup(stack, sleep=lambda _: None)
        self.assertEqual(set(containers), {'test-checkpoint-helper'})
        self.assertFalse(checkpoint.complete_checkpoints(stack.backups))
        self.assertFalse(self.stopped)
        self.assertTrue((stack.state / 'partial-archive').exists())
        (stack.state / 'partial-archive').unlink()
        def interrupted_create(argv):
            result = runner(argv)
            if argv[:2] == ['docker', 'create']:
                signal.raise_signal(signal.SIGTERM)
            return result
        stack.runner = interrupted_create
        with self.assertRaisesRegex(RuntimeError, 'interrupted'):
            stack.helper([], 'true')
        self.assertEqual(set(containers), {'test-checkpoint-helper'})
        self.assertNotEqual(names[0], names[1])
        self.assertFalse((stack.state / 'partial-archive').exists())
        # A name collision must never authorize deleting a container with another owner.
        token = names[-1].rsplit('-', 1)[1]
        containers[names[-1]] = containers['test-checkpoint-helper']
        def collision(argv):
            if argv[:2] == ['docker', 'create']:
                return subprocess.CompletedProcess(argv, 1, '', 'name already in use')
            return runner(argv)
        stack.runner = collision
        with patch.object(checkpoint.uuid, 'uuid4', return_value=SimpleNamespace(hex=token)), \
                self.assertRaisesRegex(RuntimeError, 'operation failed'), \
                contextlib.redirect_stderr(io.StringIO()):
            stack.helper([], 'true')
        self.assertEqual(containers[names[-1]]['Id'], 'foreign')

    def test_resume_child_survives_process_group_signals_and_finally_gap(self):
        child = self.stack.state / 'child.py'
        completed = self.stack.state / 'child-completed'
        child.write_text(
            "import os, signal, sys, time\n"
            "from pathlib import Path\n"
            "def interrupted(signum, frame): sys.exit(89)\n"
            "for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):\n"
            "    signal.signal(sig, interrupted)\n"
            "    os.killpg(os.getppid(), sig)\n"
            "    time.sleep(0.02)\n"
            f"Path({str(completed)!r}).write_text('finished')\n")
        program = f"""
import signal, sys
sys.path[:0] = [{str(Path(__file__).resolve().parent)!r}, {str(checkpoint.ROOT / 'scripts')!r}]
from test_checkpoint import BackupAttestationTests
import checkpoint
case = BackupAttestationTests()
case.setUp()
stack = case.stack
def resume(stopped, **kwargs):
    stack.command = [sys.executable, {str(child)!r}]
    stack.runner = checkpoint.bootstrap.run
    checkpoint.Stack.resume(stack, stopped, timeout=5)
stack.resume = resume
line = next(i for i, text in enumerate(open(checkpoint.__file__), 1) if text.strip() == 'resuming = True')
def trace(frame, event, arg):
    if event == 'line' and frame.f_code.co_filename == checkpoint.__file__ and frame.f_lineno == line:
        signal.raise_signal(signal.SIGTERM)
    return trace
sys.settrace(trace)
try:
    checkpoint.backup(stack, sleep=lambda _: None)
except RuntimeError as error:
    assert 'interrupted' in str(error), error
else:
    raise AssertionError('finally-gap signal did not interrupt capture')
finally:
    sys.settrace(None)
    case.doCleanups()
"""
        result = subprocess.run([sys.executable, '-c', program], text=True, capture_output=True,
                                start_new_session=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(completed.read_text(), 'finished')

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
        gap_line = next(i for i, line in enumerate(Path(checkpoint.__file__).read_text().splitlines(), 1)
                        if line.strip() == 'resuming = True')
        def trace(frame, event, arg):
            if event == 'line' and frame.f_code.co_filename == checkpoint.__file__ and frame.f_lineno == gap_line:
                for sig in signals:
                    signal.raise_signal(sig)
            return trace
        with self.assertRaisesRegex(RuntimeError, 'capture failed first'), contextlib.redirect_stderr(io.StringIO()) as diagnostic:
            sys.settrace(trace)
            try:
                checkpoint.backup(self.stack, sleep=lambda _: None)
            finally:
                sys.settrace(None)
        self.assertIn('service resumption also failed', diagnostic.getvalue())
        self.assertNotIn('private upstream detail', diagnostic.getvalue())
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
        for name in ('compose.yaml', 'compose.s3.yaml', 'compose.proxy.yaml', 'config.alloy'):
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
        self.stack.config = {'services': {'loki': {'image': PIN}},
                             'volumes': {'loki-data': {'name': 'recovery-test_loki-data'}}}
        self.stack.runner = image_result
        self.publish(self.source)

    def publish(self, source):
        (source / 'manifest.json').write_text(json.dumps(checkpoint.manifest(source, self.env, 'filesystem', {'loki': PIN})))

    def test_verify_checkpoint_rejects_corruption_extra_files_symlinks_pins_and_config(self):
        checkpoint.verify_checkpoint(self.stack, self.source)
        for defect in ('hash', 'extra', 'symlink', 'pins', 'identity', 'mutable', 'config'):
            with self.subTest(defect=defect):
                source = self.root / defect
                shutil.copytree(self.source, source)
                if defect == 'hash':
                    (source / 'loki-data.tar').write_bytes(b'corrupt')
                elif defect == 'extra':
                    (source / 'extra').write_text('unexpected')
                elif defect == 'symlink':
                    (source / 'extra').symlink_to(self.env)
                elif defect in ('pins', 'identity', 'mutable'):
                    doc = json.loads((source / 'manifest.json').read_text())
                    doc['images'] = {'loki': PIN.replace('a' * 64, 'c' * 64)} if defect == 'identity' else (
                        {'loki': 'example/loki:mutable'} if defect == 'mutable' else ['different-pin'])
                    (source / 'manifest.json').write_text(json.dumps(doc))
                else:
                    (source / 'configuration/config.alloy').write_text('different config')
                    self.publish(source)
                self.stack.check_empty = Mock()
                with self.assertRaises(RuntimeError):
                    checkpoint.restore(self.stack, source)
                self.stack.check_empty.assert_not_called()
                self.assertFalse(self.stack.state.exists())
        self.stack.config['services']['loki']['image'] = 'example/loki:equivalent-tag'
        checkpoint.verify_checkpoint(self.stack, self.source)
        legacy = self.root / 'legacy'
        shutil.copytree(self.source, legacy)
        defaults = checkpoint.bootstrap.images(self.checkout / 'compose.yaml')
        for path in (self.checkout / 'compose.yaml', legacy / 'configuration/compose.yaml'):
            path.write_text(checkpoint.re.sub(r'\$\{OB_[A-Z]+_IMAGE:-(.+?)\}', r'\1', path.read_text()))
        self.stack.config['services']['loki']['image'] = defaults['loki']
        doc = checkpoint.manifest(legacy, self.env, 'filesystem', list(defaults.values()))
        doc['version'] = 1
        (legacy / 'manifest.json').write_text(json.dumps(doc))
        checkpoint.verify_checkpoint(self.stack, legacy)
        shutil.copy2(self.source / 'configuration/compose.yaml', self.checkout / 'compose.yaml')
        self.stack.config['services']['loki']['image'] = PIN
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
            if argv[:3] == ['docker', 'image', 'inspect']:
                return image_result(argv)
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
