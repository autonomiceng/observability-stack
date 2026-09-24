"""Review regressions at CLI and smoke boundaries; no Docker calls."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
import bootstrap
import smoke_assertions
import smoke_s3


class ReviewFixTests(unittest.TestCase):
    def test_render_without_docker_refuses_installation_but_allows_scratch(self):
        template = Path(bootstrap.__file__).resolve().parent.parent / '.env.example'
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = root / '.env'
            (root / 'compose.yaml').write_bytes((template.parent / 'compose.yaml').read_bytes())
            with patch.object(bootstrap, '__file__', str(root / 'scripts/bootstrap.py')), \
                 patch.object(bootstrap.shutil, 'which', return_value=None), \
                 patch.object(bootstrap.subprocess, 'run', side_effect=FileNotFoundError('docker')) as run, \
                 patch.object(sys, 'argv', ['bootstrap.py', '--render-only', '--template', str(template)]), \
                 contextlib.redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(bootstrap.main(), 1)
                self.assertEqual(json.loads(errors.getvalue())['error'], 'docker_missing')
                self.assertFalse(env.exists())
                self.assertEqual(bootstrap.bootstrap(['--render-only', '--template', str(template),
                                                      '--env-file', str(root / 'scratch.env')]), 0)
                run.assert_not_called()

    def test_smoke_cleanup_preserves_ingestion_error_and_reports_cleanup_only_failure(self):
        for ingestion_fails in (True, False):
            with self.subTest(ingestion_fails=ingestion_fails), tempfile.TemporaryDirectory() as tmp:
                state = Path(tmp)
                (state / 'textfile').mkdir()
                calls = []
                def run(argv, **kwargs):
                    calls.append(argv)
                    if argv[:2] == ['docker', 'compose']:
                        output = json.dumps({'services': {'caddy': {'image': 'pinned'}}})
                    elif argv[:2] == ['docker', 'run']:
                        output = 'captured-producer-id'
                    else:
                        if kwargs.get('check'):
                            raise subprocess.CalledProcessError(1, argv)
                        return subprocess.CompletedProcess(argv, 1, b'', b'private diagnostics')
                    return subprocess.CompletedProcess(argv, 0, output, '')
                eventually = Mock(side_effect=AssertionError('original ingestion failure')) if ingestion_fails else \
                    Mock(side_effect=[{'data': {'result': [{'value': [123, '42']}]}},
                                      {'data': {'result': [{'stream': {'service': 'caddy'}, 'values': [['123', 'marker']]}]}}])
                expected = AssertionError if ingestion_fails else RuntimeError
                with patch('subprocess.run', side_effect=run), \
                     patch.object(smoke_assertions, 'client', return_value=(None, None, eventually)), \
                     patch.object(smoke_assertions, 'verify_marker'), contextlib.redirect_stderr(io.StringIO()) as errors:
                    with self.assertRaisesRegex(expected, 'original ingestion failure' if ingestion_fails else 'cleanup'):
                        smoke_assertions.ingest_marker(state / '.env', 'localhost:80', state)
                self.assertIn(['docker', 'rm', '-f', 'captured-producer-id'], calls)
                self.assertFalse((state / 'textfile/persistence-marker.prom').exists())
                self.assertNotIn('private diagnostics', errors.getvalue())

    def test_s3_smoke_keeps_docker_ingestion_and_requires_trace_after_restart(self):
        stack = Mock(mode='s3')
        proof = {'marker': 'marker', 'timestamp': 123}
        trace = {'trace_id': 'trace-id', 'span_name': 'span'}
        events = []
        stack.dc.side_effect = lambda *args: events.append(args[0])
        with patch.object(smoke_s3, 'Stack', return_value=stack), \
             patch.object(smoke_s3, 'ingest_marker', return_value=proof) as ingest, \
             patch.object(smoke_s3, 'verify_marker') as verify_marker, \
             patch.object(smoke_s3.recovery_assertions, 'ingest_trace', return_value=trace), \
             patch.object(smoke_s3.recovery_assertions, 'flush_s3', return_value={'objects': 'fixture'}) as flush, \
             patch.object(smoke_s3.recovery_assertions, 's3_inventory', return_value={'objects': 'fixture'}) as inventory, \
             patch.object(smoke_s3.recovery_assertions, 'verify_objects'), \
             patch.object(smoke_s3.recovery_assertions, 'verify_trace') as verify_trace:
            def trace_query(*args):
                if 'restart' in events:
                    raise AssertionError('historical trace missing after restart')
                events.append('trace-verified')
            verify_trace.side_effect = trace_query
            with self.assertRaisesRegex(AssertionError, 'historical trace missing'):
                smoke_s3.check(Path('/unused.env'), 'localhost:80')
            ingest.assert_called_once_with(Path('/unused.env'), 'localhost:80', stack.state)
            self.assertLess(events.index('trace-verified'), events.index('restart'))
            self.assertEqual(verify_trace.call_count, 2)
            self.assertEqual(flush.call_args.kwargs['buckets'], ('loki', 'mimir-blocks', 'tempo'))
            self.assertEqual(inventory.call_args.kwargs['buckets'], ('loki', 'mimir-blocks', 'tempo'))
            verify_marker.assert_called_once()


    def test_s3_flush_requires_tempo_objects_before_restart(self):
        stack = Mock()
        buckets = ('loki', 'mimir-blocks', 'tempo')
        objects = {'loki': {'chunk': 'a'}, 'mimir-blocks': {'block': 'b'}, 'tempo': {}}
        with patch.object(smoke_s3.recovery_assertions, 's3_inventory', return_value=objects) as inventory, \
             patch.object(smoke_s3.recovery_assertions.time, 'monotonic', side_effect=[0, 181]):
            with self.assertRaisesRegex(RuntimeError, 'did not persist objects.*tempo'):
                smoke_s3.recovery_assertions.flush_s3(stack, buckets=buckets)
            inventory.assert_called_once_with(stack, buckets=buckets)
        objects['tempo']['trace-block'] = 'c'
        with patch.object(smoke_s3.recovery_assertions, 's3_inventory', return_value=objects):
            self.assertEqual(smoke_s3.recovery_assertions.flush_s3(stack, buckets=buckets), objects)
