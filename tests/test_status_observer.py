"""Host observer acceptance at fake Docker and real publication boundaries."""

import copy
import itertools
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
import status_config as config_module
import status_observer as observer
from status_io import Unavailable, task_record

ROOT = Path(__file__).resolve().parent.parent
AT = '2026-09-20T12:00:00Z'
START = '2026-09-20T11:00:00Z'
DIGEST = 'sha256:' + 'a' * 64
IMAGE_ID = 'sha256:' + 'b' * 64


class FakeDocker:
    def __init__(self, root):
        self.root = root
        self.config = {'name': 'selected', 'services': {}, 'networks': {'default': {'name': 'selected_default'}}}
        self.docs, self.calls = {}, []
        self.fail_config = self.fail_inventory = False
        self.bad_component = None
        self.endpoint = 'unix:///var/run/docker.sock'
        self.security = ['name=seccomp,profile=builtin']
        for index, name in enumerate(observer.SERVICES):
            self.config['services'][name] = {'image': 'private/repo:1.2.3@' + DIGEST, 'volumes': []}
            cid = f'{index + 1:064x}'
            self.docs[cid] = {
                'Id': cid, 'Image': IMAGE_ID,
                'Config': {'Labels': {'com.docker.compose.project': 'selected',
                                      'com.docker.compose.service': name, 'com.docker.compose.oneoff': 'False'},
                           'Env': ['PASSWORD=SECRET']},
                'State': {'Status': 'running', 'StartedAt': START, 'Paused': False,
                          'Health': {'Status': 'healthy', 'Log': ['SECRET']}},
                'NetworkSettings': {'Networks': {'selected_default': {'IPAddress': '172.20.0.2'}}}}
        self.config['services']['caddy']['environment'] = {'OB_PUBLIC_DOMAIN': 'localhost', 'TOKEN': 'SECRET'}
        self.bind('caddy', root / 'custom-state/console', '/srv/state')
        self.bind('alloy', ROOT / 'config.alloy', '/etc/alloy/config.alloy')
        self.config['services']['alloy']['command'] = ['run', '--server.http.listen-addr=0.0.0.0:12345',
                                                      '--storage.path=/var/lib/alloy', '/etc/alloy/config.alloy']
        for name in ('loki', 'mimir', 'tempo'):
            self.bind(name, ROOT / f'docker/{name}/config.yaml', f'/etc/{name}/config.yaml')
            self.config['services'][name]['command'] = [f'-config.file=/etc/{name}/config.yaml', '-config.expand-env=true']
            if name != 'loki':
                self.config['services'][name]['command'].insert(0, '-target=all')

    def bind(self, name, source, target):
        self.config['services'][name]['volumes'].append(
            {'source': str(source), 'target': target, 'type': 'bind', 'read_only': True})

    def __call__(self, argv, **options):
        self.calls.append((argv, options))
        if argv[:2] == ['docker', 'compose']:
            if self.fail_config:
                raise Unavailable('SECRET')
            return json.dumps(self.config)
        if argv[:2] == ['docker', 'ps']:
            if self.fail_inventory:
                raise Unavailable('SECRET')
            return '\n'.join(cid + ' ' + doc['Config']['Labels']['com.docker.compose.service'] + ' False'
                             for cid, doc in self.docs.items())
        if argv[:3] == ['docker', 'context', 'inspect']:
            return json.dumps([{'Endpoints': {'docker': {'Host': self.endpoint}}}])
        if argv[:2] == ['docker', 'info']:
            return json.dumps(self.security)
        if argv[:3] == ['docker', 'network', 'inspect']:
            return '[{"Driver":"bridge","Scope":"local"}]'
        if argv[:2] == ['docker', 'inspect']:
            doc = self.docs[argv[2]]
            if doc['Config']['Labels']['com.docker.compose.service'] == self.bad_component:
                return '{"unexpected":"SECRET"}'
            return json.dumps([doc])
        raise AssertionError(argv)


class ObserverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'compose.yaml').write_text('name: selected')
        self.env = self.root / 'selected.env'
        self.env.write_text('OB_STATE_DIR=./custom-state\n')
        self.fake = FakeDocker(self.root)
        self.probe = patch.object(observer, 'probe', return_value=('healthy', '9.8.7')).start()
        self.addCleanup(patch.stopall)
        self.public = self.root / 'custom-state/console/status.json'

    def observe(self, clock=lambda: AT):
        return observer.observe(self.root, self.env, self.fake, clock)

    def rows(self, doc):
        return {row['id']: row for row in doc['components']}

    def test_exact_public_allowlist_and_configuration_runtime_identity_separation(self):
        doc = self.observe()
        self.assertEqual(len(doc['components']), 9)
        self.assertEqual(doc['telemetry'], 'configured')
        self.assertEqual(doc['stack'], 'observability')
        self.assertEqual(doc['configurationValidForSeconds'], 120)
        row = self.rows(doc)['caddy']
        self.assertEqual(row['configuredVersion'], '1.2.3')
        self.assertEqual(row['observedVersion'], '9.8.7')
        self.assertEqual(row['configuredDigest'], DIGEST)
        self.assertEqual(row['observedImageId'], IMAGE_ID)
        self.assertLess(len(self.public.read_bytes()), 65536)
        public = self.public.read_text()
        for private in ('SECRET', 'selected', str(self.root), '172.20', 'private/repo', 'Config', 'Health'):
            self.assertNotIn(private, public)
        self.assertEqual(set(doc), {'schemaVersion', 'stack', 'generatedAt', 'configurationObservedAt',
                                    'configurationValidForSeconds', 'telemetry', 'components'})
        allowed = {'id', 'kind', 'configured', 'state', 'observedAt', 'validForSeconds',
                   'configuredVersion', 'observedVersion', 'configuredDigest', 'observedImageId', 'lastExecutionAt'}
        self.assertTrue(all(set(row) <= allowed for row in doc['components']))

    def test_explicit_selection_and_shell_overrides_are_removed_from_every_call(self):
        with patch.dict(os.environ, {'OB_STATE_DIR': '/wrong', 'OB_CADDY_IMAGE': 'SECRET',
                                     'COMPOSE_PROJECT_NAME': 'wrong', 'COMPOSE_FILE': '/wrong', 'HTTP_PROXY': 'SECRET'}):
            self.observe()
        argv = self.fake.calls[0][0]
        self.assertEqual(argv[argv.index('--env-file') + 1], str(self.env))
        for _, options in self.fake.calls:
            self.assertEqual(options['cwd'], self.root)
            self.assertFalse(any(k.startswith(('OB_', 'COMPOSE_')) for k in options['env']))
            self.assertNotIn('HTTP_PROXY', options['env'])

    def test_component_timestamps_do_not_reuse_configuration_or_assembly_time(self):
        lock, ticks = threading.Lock(), itertools.count()
        def clock():
            with lock:
                return f'2026-09-20T12:00:{next(ticks):02d}Z'
        doc = self.observe(clock)
        times = [row['observedAt'] for row in doc['components'] if row['kind'] == 'service']
        self.assertEqual(len(times), len(set(times)))
        self.assertNotIn(doc['configurationObservedAt'], times)
        self.assertNotIn(doc['generatedAt'], times)

    def test_configuration_failure_leaves_success_bytes_and_mtime_frozen(self):
        self.observe()
        old, mtime = self.public.read_bytes(), self.public.stat().st_mtime_ns
        self.fake.fail_config = True
        with self.assertRaises(Unavailable):
            self.observe(lambda: '2026-09-20T12:10:00Z')
        self.assertEqual(self.public.read_bytes(), old)
        self.assertEqual(self.public.stat().st_mtime_ns, mtime)

    def test_bad_inspection_and_bad_probe_leave_valid_siblings(self):
        self.fake.bad_component = 'grafana'
        def probe(name, *args):
            if name == 'mimir':
                raise RuntimeError('SECRET')
            return 'healthy', '9.8.7'
        self.probe.side_effect = probe
        rows = self.rows(self.observe())
        self.assertEqual(rows['grafana']['state'], 'unknown')
        self.assertEqual(rows['mimir']['state'], 'unknown')
        self.assertEqual(rows['loki']['state'], 'healthy')
        self.assertNotIn('SECRET', self.public.read_text())

    def test_failed_or_empty_inventory_cannot_establish_installation_absence(self):
        self.fake.fail_inventory = True
        self.assertEqual(self.rows(self.observe())['loki']['state'], 'unknown')
        self.fake.fail_inventory = False
        self.fake.docs.clear()
        self.assertEqual(self.rows(self.observe())['loki']['state'], 'unknown')
        self.assertEqual(self.rows(self.observe())['rustfs-init']['state'], 'unknown')

    def test_runtime_failure_replaces_old_success_without_renewing_its_facts(self):
        self.observe()
        self.probe.return_value = ('unavailable', None)
        row = self.rows(self.observe(lambda: '2026-09-20T12:01:00Z'))['loki']
        self.assertEqual(row['state'], 'unavailable')
        self.assertEqual(row['observedAt'], '2026-09-20T12:01:00Z')
        self.assertNotIn('observedVersion', row)

    def test_health_label_and_running_alone_do_not_establish_readiness(self):
        self.probe.return_value = ('unknown', None)
        self.assertEqual(self.rows(self.observe())['loki']['state'], 'unknown')

    def test_remote_and_rootless_skip_ip_probes(self):
        for endpoint, security in [('ssh://remote', []), ('unix:///run/user/1000/docker.sock', ['name=rootless'])]:
            self.fake.endpoint, self.fake.security = endpoint, security
            self.probe.reset_mock()
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(self.rows(self.observe())['loki']['state'], 'unknown')
            self.probe.assert_not_called()

    def test_conflicting_docker_host_and_context_cannot_enable_ip_probes(self):
        cases = [
            ('tcp://remote.example:2375', 'local-rootful', 'unix:///var/run/docker.sock'),
            ('unix:///var/run/docker.sock', 'remote', 'ssh://remote.example'),
            ('unix:///var/run/docker.sock', 'other-local', 'unix:///run/docker.sock'),
        ]
        for docker_host, docker_context, inspected in cases:
            with self.subTest(docker_host=docker_host, docker_context=docker_context,
                              inspected=inspected):
                self.fake.endpoint = inspected
                self.probe.reset_mock()
                with patch.dict(os.environ, {'DOCKER_HOST': docker_host,
                                             'DOCKER_CONTEXT': docker_context}, clear=True):
                    self.assertEqual(self.rows(self.observe())['loki']['state'], 'unknown')
                self.probe.assert_not_called()

    def test_consistent_local_docker_host_and_context_allow_ip_probes(self):
        self.fake.endpoint = 'unix:///var/run/docker.sock'
        with patch.dict(os.environ, {'DOCKER_HOST': self.fake.endpoint,
                                     'DOCKER_CONTEXT': 'local-rootful'}, clear=True):
            self.assertEqual(self.rows(self.observe())['loki']['state'], 'healthy')
        self.probe.assert_called()

    def test_rustfs_disabled_requires_marker_and_reviewed_backend_configs(self):
        self.fake.config['services'].pop('rustfs')
        self.assertEqual(self.rows(self.observe())['rustfs']['state'], 'unknown')
        marker = self.root / 'custom-state/installation/storage-mode'
        marker.parent.mkdir()
        marker.write_text('filesystem\n')
        row = self.rows(self.observe())['rustfs']
        self.assertEqual((row['configured'], row['state'], row['observedAt']), (False, 'disabled', AT))
        self.assertEqual(self.rows(self.observe())['rustfs-init']['state'], 'disabled')
        self.fake.config['services']['loki']['volumes'][0]['source'] = str(ROOT / 'docker/loki/s3.yaml')
        self.assertEqual(self.rows(self.observe())['rustfs']['state'], 'unknown')

    def test_telemetry_requires_actual_current_file_and_expected_command(self):
        source = self.root / 'config.alloy'
        source.write_text('// loki.source.docker prometheus.remote_write otelcol.receiver.otlp\n')
        self.fake.config['services']['alloy']['volumes'][0]['source'] = str(source)
        self.assertEqual(self.observe()['telemetry'], 'unknown')
        shutil.copyfile(ROOT / 'config.alloy', source)
        self.assertEqual(self.observe()['telemetry'], 'configured')
        self.fake.config['services']['alloy']['command'] = ['run', '/another-file']
        self.assertEqual(self.observe()['telemetry'], 'unknown')

    def test_bootstrap_record_dates_and_unknown_interrupted_execution(self):
        task_record(self.root / 'custom-state', self.root, self.env, START, 'healthy')
        row = self.rows(self.observe())['bootstrap']
        self.assertEqual((row['state'], row['lastExecutionAt'], row['observedAt']), ('healthy', START, AT))
        task_record(self.root / 'custom-state', self.root, self.env, START, 'unknown')
        self.assertEqual(self.rows(self.observe())['bootstrap']['state'], 'unknown')

    def test_symlinked_checkout_and_env_match_canonical_bootstrap_record(self):
        links = tempfile.TemporaryDirectory()
        self.addCleanup(links.cleanup)
        link = Path(links.name) / 'checkout'
        link.symlink_to(self.root, target_is_directory=True)
        task_record(self.root / 'custom-state', self.root.resolve(), self.env.resolve(),
                    START, 'healthy')
        document = observer.observe(link, link / self.env.name, self.fake, lambda: AT)
        row = self.rows(document)['bootstrap']
        self.assertEqual((row['state'], row['lastExecutionAt']), ('healthy', START))
        self.assertTrue(all(options['cwd'] == self.root.resolve()
                            for _, options in self.fake.calls))

    def test_task_success_failure_running_and_future_start(self):
        self.fake.config['services']['rustfs-init'] = {'image': 'private/SECRET'}
        cid = 'f' * 64
        task = copy.deepcopy(next(iter(self.fake.docs.values())))
        task['Id'] = cid
        task['Config']['Labels']['com.docker.compose.service'] = 'rustfs-init'
        self.fake.docs[cid] = task
        for status, code, expected in [('exited', 0, 'healthy'), ('exited', 1, 'unavailable'), ('running', 0, 'starting')]:
            task['State'] = {'Status': status, 'ExitCode': code, 'StartedAt': START, 'FinishedAt': AT}
            row = self.rows(self.observe())['rustfs-init']
            self.assertEqual((row['state'], row['lastExecutionAt']), (expected, START))
        task['State']['StartedAt'] = '2026-09-21T12:00:00Z'
        self.assertIsNone(self.rows(self.observe())['rustfs-init']['lastExecutionAt'])

    def test_restart_during_probe_discards_mixed_runtime_evidence(self):
        def probe(name, *args):
            if name == 'loki':
                for doc in self.fake.docs.values():
                    if doc['Config']['Labels']['com.docker.compose.service'] == name:
                        doc['State']['StartedAt'] = AT
            return 'healthy', '9.8.7'
        self.probe.side_effect = probe
        row = self.rows(self.observe())['loki']
        self.assertEqual(row['state'], 'unknown')
        self.assertNotIn('observedVersion', row)
        self.assertNotIn('observedImageId', row)

    def test_component_limit_prevents_publication(self):
        self.observe()
        old = self.public.read_bytes()
        with patch.object(observer, 'collect', return_value={'components': [{}] * 33}):
            with self.assertRaises(Unavailable):
                self.observe()
        self.assertEqual(self.public.read_bytes(), old)

    def test_concurrent_publication_is_a_noop_and_preserves_existing_document(self):
        import fcntl
        self.observe()
        before = self.public.read_bytes()
        lock = self.root / 'custom-state/status/observer.lock'
        with lock.open('w') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(observer, 'collect', side_effect=AssertionError('duplicate collection')):
                self.assertIsNone(self.observe())
        self.assertEqual(self.public.read_bytes(), before)
