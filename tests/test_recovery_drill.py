"""Recovery isolation, producer lifetime and object-integrity regressions; no Docker."""
import importlib.util
import contextlib
import io
import subprocess
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
import checkpoint
import recovery_assertions

spec = importlib.util.spec_from_file_location('backup_drill', checkpoint.ROOT / 'scripts/backup-drill.py')
drill = importlib.util.module_from_spec(spec)
spec.loader.exec_module(drill)


class RecoveryDrillTests(unittest.TestCase):
    def test_profiles_use_isolated_checkout_without_host_discovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            source_config = (checkpoint.ROOT / 'config.alloy').read_bytes()
            source_compose = (checkpoint.ROOT / 'compose.yaml').read_bytes()
            root = drill.prepare_checkout(checkpoint.ROOT, Path(tmp) / 'checkout')
            config = (root / 'config.alloy').read_text()
            compose = (root / 'compose.yaml').read_text()
            for component in ('discovery.docker', 'loki.source.docker', 'prometheus.exporter.cadvisor'):
                self.assertNotIn(component, config)
            for host_path in ('/rootfs:', '/var/run/docker.sock:', '/var/lib/docker:', 'privileged: true'):
                self.assertNotIn(host_path, compose)
            self.assertIn('prometheus.remote_write', config)
            self.assertIn('otelcol.receiver.otlp', config)
            self.assertIn('set_collectors = ["textfile"]', config)
            self.assertEqual((checkpoint.ROOT / 'config.alloy').read_bytes(), source_config)
            self.assertEqual((checkpoint.ROOT / 'compose.yaml').read_bytes(), source_compose)
            for profile, files in [('filesystem', ['compose.yaml']), ('s3', ['compose.yaml', 'compose.s3.yaml'])]:
                settings = drill.profile_settings(profile)
                self.assertEqual(settings['COMPOSE_FILE'].split(':'), files)
                self.assertTrue(all((root / name).exists() for name in files))
            with patch.dict(os.environ, {'SMOKE_PROFILE': 'typo'}), patch.object(checkpoint, 'checked') as run:
                with self.assertRaisesRegex(RuntimeError, 'SMOKE_PROFILE'):
                    drill.main()
                run.assert_not_called()

    def test_cleanup_preserves_ingestion_failure_and_removes_producers(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            (state / 'textfile').mkdir()
            stack = SimpleNamespace(env_file=state / '.env', state=state, dc=Mock())
            metric = state / 'textfile/persistence-marker.prom'
            def ingested(path, predicate):
                self.assertTrue(metric.exists())
                result = {'data': {'result': [{'value': [1234, '42']}]}}
                self.assertTrue(predicate(result))
                return result
            for fail in (False, True):
                with self.subTest(fail=fail):
                    api = Mock()
                    eventually = Mock(side_effect=RuntimeError('ingestion failed') if fail else ingested)
                    with patch.object(recovery_assertions, 'client', return_value=(None, api, eventually)), \
                         patch.object(recovery_assertions, 'verify') as verify:
                        if fail:
                            with self.assertRaisesRegex(RuntimeError, 'ingestion failed'):
                                recovery_assertions.ingest(stack, 'localhost:18190')
                        else:
                            proof = recovery_assertions.ingest(stack, 'localhost:18190')
                            self.assertEqual(proof['query_time'], 1234)
                            verify.assert_called_once()
                            calls = stack.dc.call_args_list
                            self.assertTrue(any('http://loki:3100/loki/api/v1/push' in call.args for call in calls))
                            trace_call = next(call for call in calls if 'http://ob-alloy-otlp:4318/v1/traces' in call.args)
                            payload = json.loads(next(arg.removeprefix('--post-data=') for arg in trace_call.args
                                                      if arg.startswith('--post-data=')))
                            self.assertEqual(payload['resourceSpans'][0]['scopeSpans'][0]['spans'][0]['traceId'], proof['trace_id'])
                        self.assertFalse(metric.exists())

        calls = []
        def run(argv):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 1 if 'down' in argv else 0, '', '')
        with tempfile.TemporaryDirectory() as tmp, patch.object(checkpoint.bootstrap, 'run', side_effect=run), \
             contextlib.redirect_stderr(io.StringIO()) as diagnostics:
            work = Path(tmp)
            with self.assertRaisesRegex(RuntimeError, 'original ingestion failure'):
                try:
                    raise RuntimeError('original ingestion failure')
                finally:
                    self.assertTrue(drill.cleanup(['docker', 'compose'], 'observability-drill-test',
                                                 'drill-network', True, work, False))
            self.assertTrue(work.exists())
            self.assertIn('artifacts retained', diagnostics.getvalue())
            self.assertTrue(any(argv[:3] == ['docker', 'volume', 'rm'] for argv in calls))
            self.assertIn(['docker', 'network', 'rm', 'drill-network'], calls)

    def test_s3_recovery_rejects_missing_or_changed_object_bodies(self):
        stack = SimpleNamespace(command=['docker', 'compose'], dc=Mock(return_value=
            '<ListBucketResult><IsTruncated>false</IsTruncated>'
            '<Contents><Key>data/chunk</Key><ETag>unchanged-metadata</ETag></Contents></ListBucketResult>'))
        def response(body, code=0):
            def run(argv, stdout, stderr):
                stdout.write(body)
                return subprocess.CompletedProcess(argv, code, None, b'')
            return run
        with patch.object(recovery_assertions.subprocess, 'run', side_effect=response(b'original\xff\n')):
            before = recovery_assertions.s3_inventory(stack)
            after = recovery_assertions.s3_inventory(stack)
        recovery_assertions.verify_objects(before, after)
        with patch.object(recovery_assertions.subprocess, 'run', side_effect=response(b'corrupted\xff\n')):
            corrupt = recovery_assertions.s3_inventory(stack)
        for after in ({}, corrupt, {'loki': before['loki'], 'mimir-blocks': {}}):
            with self.subTest(after=after), self.assertRaisesRegex(RuntimeError, 'object bodies differ'):
                recovery_assertions.verify_objects(before, after)
        with patch.object(recovery_assertions.subprocess, 'run', side_effect=response(b'partial', 1)):
            with self.assertRaisesRegex(RuntimeError, 'could not read'):
                recovery_assertions.s3_inventory(stack)
