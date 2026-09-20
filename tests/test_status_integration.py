"""Opt-in installer and bootstrap task integration, with fake external commands."""

import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
import bootstrap
import install_status_timer as installer
from status_io import Unavailable
from test_bootstrap import runner_with

ROOT = Path(__file__).resolve().parent.parent


class StatusIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        shutil.copytree(ROOT / 'docker', self.root / 'docker')
        shutil.copyfile(ROOT / 'compose.yaml', self.root / 'compose.yaml')
        (self.root / 'scripts').mkdir(mode=0o755)
        (self.root / 'scripts/status_observer.py').write_text('# fake checkout\n')

    def test_timer_is_user_oneshot_30_seconds_and_installer_refuses_reselection(self):
        env = self.root / 'chosen.env'
        env.touch()
        calls = []
        units = self.root / 'units'
        installer.install(self.root, env, units, lambda argv, **kw: calls.append(argv))
        service = (units / 'observability-status.service').read_text()
        timer = (units / 'observability-status.timer').read_text()
        self.assertIn('Type=oneshot', service)
        self.assertIn('TimeoutStartSec=90', service)
        self.assertIn(str(env), service)
        self.assertIn('OnUnitInactiveSec=30s', timer)
        self.assertEqual(calls, [['systemctl', '--user', 'daemon-reload'],
                                 ['systemctl', '--user', 'enable', '--now', 'observability-status.timer']])
        with self.assertRaises(Unavailable):
            installer.install(self.root, env, units, lambda *args, **kw: self.fail('reselected'))

    def test_systemd_quotes_paths_without_shell_expansion(self):
        value = installer.quote('/tmp/a "$SECRET" %h\\b')
        self.assertIn('$$SECRET', value)
        self.assertIn('%%h', value)
        self.assertIn('\\"', value)
        self.assertIn('\\\\', value)
        with self.assertRaises(Unavailable):
            installer.quote('/tmp/injected\nExecStart=secret')

    def bootstrap_run(self, fail_ready=False, fail_observer=False):
        base = runner_with()
        seen = []
        def run(argv):
            if argv[0] == sys.executable:
                record = json.loads((self.root / 'custom/status/bootstrap.json').read_text())
                self.assertEqual(record['state'], 'healthy')
                self.assertGreater(len(seen), 0)
                self.assertEqual(argv[-4:], ['--checkout', str(self.root), '--env-file', str(self.root / '.env')])
                return subprocess.CompletedProcess(argv, int(fail_observer), '', 'SECRET')
            return base(argv)
        def ready(*args, **kw):
            seen.append(args)
            record = json.loads((self.root / 'custom/status/bootstrap.json').read_text())
            self.assertEqual(record['state'], 'unknown')
            if fail_ready:
                raise bootstrap.Refused('not_ready', 'private failure')
        with patch.object(bootstrap, '__file__', str(self.root / 'scripts/bootstrap.py')), \
                patch.object(bootstrap, 'wait_ready', side_effect=ready), \
                patch.dict(os.environ, {'OB_STATE_DIR': str(self.root / 'custom'), 'OB_ALERTS': 'placeholder'}), \
                patch('sys.stdout', new_callable=io.StringIO), patch('sys.stderr', new_callable=io.StringIO) as errors:
            if fail_ready:
                with self.assertRaises(bootstrap.Refused):
                    bootstrap.bootstrap(['--template', str(ROOT / '.env.example')], runner=run)
            else:
                self.assertEqual(bootstrap.bootstrap(['--template', str(ROOT / '.env.example')], runner=run), 0)
            self.assertNotIn('SECRET', errors.getvalue())
        return json.loads((self.root / 'custom/status/bootstrap.json').read_text())

    def test_bootstrap_success_record_precedes_initial_observer_after_ready(self):
        record = self.bootstrap_run()
        self.assertEqual(record['state'], 'healthy')
        self.assertEqual((self.root / 'custom/status').stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.root / 'custom/status/bootstrap.json').stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.root / 'custom/console').stat().st_mode & 0o777, 0o755)
        saved = (self.root / '.env').read_text()
        self.assertIn('OB_STATE_DIR=' + str(self.root / 'custom'), saved)
        self.assertIn('COMPOSE_PROJECT_NAME=', saved)

    def test_bootstrap_readiness_failure_records_failed_task(self):
        self.assertEqual(self.bootstrap_run(fail_ready=True)['state'], 'unavailable')

    def test_initial_observer_failure_does_not_undo_bootstrap_success(self):
        self.assertEqual(self.bootstrap_run(fail_observer=True)['state'], 'healthy')

    def test_partial_unit_write_rolls_back_without_enabling_timer(self):
        env = self.root / '.env'
        env.touch()
        unit_dir = self.root / 'units'
        original = os.open
        def opened(path, *args, **kwargs):
            if path == installer.NAME + '.timer':
                raise OSError('injected second write failure')
            return original(path, *args, **kwargs)
        calls = []
        with patch.object(installer.os, 'open', side_effect=opened), self.assertRaises(OSError):
            installer.install(self.root, env, unit_dir, lambda *args, **kwargs: calls.append(args))
        self.assertEqual(list(unit_dir.iterdir()), [])
        self.assertEqual(calls, [])

    def test_xdg_relative_and_empty_values_use_home_config(self):
        argv = ['installer', '--checkout', str(self.root), '--env-file', str(self.root / '.env'), '--install']
        for value in ('', 'relative', str(self.root / 'absolute')):
            with patch.dict(os.environ, {'XDG_CONFIG_HOME': value}), patch.object(sys, 'argv', argv), \
                    patch.object(installer, 'install') as install, patch('builtins.print'):
                self.assertEqual(installer.main(), 0)
            expected = Path(value) if Path(value).is_absolute() else Path.home() / '.config'
            self.assertEqual(install.call_args.args[2], expected / 'systemd/user')

    def test_optional_record_failure_preserves_bootstrap_result_and_refusal(self):
        for failure in (False, True):
            with self.subTest(failure=failure), \
                    patch.object(bootstrap, '__file__', str(self.root / 'scripts/bootstrap.py')), \
                    patch.object(bootstrap, 'task_record', side_effect=PermissionError('SECRET')), \
                    patch.object(bootstrap, 'wait_ready', side_effect=bootstrap.Refused('not_ready', 'original') if failure else None), \
                    patch.dict(os.environ, {'OB_STATE_DIR': str(self.root / 'custom'), 'OB_ALERTS': 'placeholder'}), \
                    patch('sys.stdout', new_callable=io.StringIO), patch('sys.stderr', new_callable=io.StringIO) as errors:
                if failure:
                    with self.assertRaises(bootstrap.Refused) as caught:
                        bootstrap.bootstrap(['--template', str(ROOT / '.env.example')], runner=runner_with())
                    self.assertEqual(str(caught.exception), 'not_ready')
                else:
                    self.assertEqual(bootstrap.bootstrap(['--template', str(ROOT / '.env.example')], runner=runner_with()), 0)
                self.assertNotIn('SECRET', errors.getvalue())

    def test_timer_activation_failure_retains_units_for_explicit_recovery(self):
        env = self.root / '.env'
        env.touch()
        unit_dir = self.root / 'units'
        with self.assertRaises(Unavailable):
            installer.install(self.root, env, unit_dir, lambda *args, **kwargs: (_ for _ in ()).throw(Unavailable()))
        self.assertEqual({path.name for path in unit_dir.iterdir()},
                         {installer.NAME + '.service', installer.NAME + '.timer'})
