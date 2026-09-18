"""Bootstrap contract. Docker is never called; a fake runner answers instead."""

import json
import os
import subprocess
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import bootstrap  # noqa: E402


def runner_with(volumes=(), network_exists=True, labelled_volumes=()):
    calls = []

    def run(argv):
        calls.append(argv)
        if argv[:3] == ["docker", "volume", "ls"]:
            if "--filter" in argv:
                project = argv[argv.index("--filter") + 1].rsplit("=", 1)[1]
                found = set(labelled_volumes) | {name for name in volumes if name.startswith(project + "_")}
            else:
                found = volumes
            return subprocess.CompletedProcess(argv, 0, "\n".join(found), "")
        if argv[:3] == ["docker", "network", "inspect"]:
            return subprocess.CompletedProcess(argv, 0 if network_exists else 1, "", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    run.calls = calls
    return run


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = self.root / ".env"
        self.template = Path(__file__).resolve().parent.parent / ".env.example"
        shutil.copytree(self.template.parent / 'docker', self.root / 'docker')
        self.alert_env = patch.dict(os.environ, {'OB_ALERTS': 'placeholder'})
        self.alert_env.start()
        self.addCleanup(self.alert_env.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def render(self, **kwargs):
        return bootstrap.bootstrap(
            ["--env-file", str(self.env), "--template", str(self.template), "--render-only"],
            **kwargs,
        )

    def test_fresh_render_generates_every_secret_once_and_locks_the_file_down(self):
        self.assertEqual(self.render(), 0)
        text = self.env.read_text()
        for key in bootstrap.MANAGED:
            self.assertEqual(text.count(f"\n{key}="), 1, key)
        self.assertEqual(oct(self.env.stat().st_mode & 0o777), "0o600")
        self.assertIn("OB_GRAFANA_ADMIN_PASSWORD=", text)
        self.assertIn("OB_S3_SECRET_KEY=", text)

    def test_second_render_keeps_existing_secrets_and_unmanaged_lines(self):
        self.render()
        before = self.env.read_text()
        self.env.write_text(before + "MY_CUSTOM=1\n")
        self.render()
        after = self.env.read_text()
        self.assertTrue(after.startswith(before))
        self.assertIn("MY_CUSTOM=1", after)

    def test_fresh_render_persists_a_managed_secret_from_the_shell(self):
        with patch.dict(os.environ, {"OB_GRAFANA_ADMIN_PASSWORD": "operator-choice"}):
            self.assertEqual(self.render(), 0)
        self.assertIn("OB_GRAFANA_ADMIN_PASSWORD=operator-choice\n", self.env.read_text())

    def test_later_render_refuses_a_conflicting_shell_secret(self):
        self.render()
        with patch.dict(os.environ, {"OB_GRAFANA_ADMIN_PASSWORD": "different"}):
            with self.assertRaises(bootstrap.Refused) as ctx:
                self.render()
        self.assertEqual(ctx.exception.code, "shell_env_conflict")
        self.assertIn("OB_GRAFANA_ADMIN_PASSWORD", ctx.exception.detail)
        self.assertNotIn("different", ctx.exception.detail)

    def test_duplicate_managed_key_is_refused_not_repaired(self):
        self.env.write_text("OB_GRAFANA_ADMIN_PASSWORD=a\nOB_GRAFANA_ADMIN_PASSWORD=b\n")
        with self.assertRaises(bootstrap.Refused) as ctx:
            self.render()
        self.assertEqual(ctx.exception.code, "env_repair_required")

    def test_installation_state_sees_state_path_and_project_volumes(self):
        data = self.root / "pg"
        data.mkdir()
        (data / "storage-mode").write_text("filesystem")
        found = bootstrap.installation_state(
            self.root, data,
            runner_with(volumes=["observability-stack_grafana-data", "observability-smoke_grafana-data", "other_x"]),
        )
        self.assertEqual(len(found), 2, "another project's volumes are not this installation")
        self.assertTrue(any("data at" in f for f in found))
        self.assertTrue(any("grafana-data" in f for f in found))

    def test_versions_json_reads_tags_from_compose(self):
        compose = Path(__file__).resolve().parent.parent / "compose.yaml"
        bootstrap.write_versions(self.root, compose)
        doc = json.loads((self.root / "data" / "console" / "versions.json").read_text())
        self.assertEqual(doc["images"]["grafana"], bootstrap.images(compose)["grafana"])
        self.assertNotIn("sha256", json.dumps(doc))
        self.assertEqual(set(doc["images"]), set(bootstrap.images(compose)))

    def test_installation_state_follows_compose_project_name(self):
        found = bootstrap.installation_state(
            self.root, self.root / "missing",
            runner_with(volumes=["observability-stack_grafana-data", "observability-smoke_grafana-data"]),
            project="observability-smoke",
        )
        self.assertEqual(found, ["volume observability-smoke_grafana-data"])

    def test_project_name_precedence_shell_then_env_file_then_default(self):
        self.assertEqual(bootstrap.project_name({}), "observability-stack")
        self.assertEqual(bootstrap.project_name({"COMPOSE_PROJECT_NAME": "'review-existing'"}), "review-existing")
        os.environ["COMPOSE_PROJECT_NAME"] = "from-shell"
        try:
            self.assertEqual(bootstrap.project_name({"COMPOSE_PROJECT_NAME": "from-file"}), "from-shell")
        finally:
            del os.environ["COMPOSE_PROJECT_NAME"]

    def test_export_and_quoted_assignments_are_read(self):
        self.env.write_text('export OB_GRAFANA_ADMIN_PASSWORD=abc\nOB_STATE_DIR="/srv/pg"\n')
        lines, values = bootstrap.read_env(self.env)
        self.assertEqual(values["OB_GRAFANA_ADMIN_PASSWORD"], "abc")
        settings = {m.group("key"): bootstrap.unquote(m.group("value")) for m in map(bootstrap.ENV_LINE.match, lines) if m}
        self.assertEqual(settings["OB_STATE_DIR"], "/srv/pg")

    def test_network_is_created_only_when_missing(self):
        run = runner_with(network_exists=False)
        bootstrap.ensure_network(run)
        self.assertEqual(run.calls[-1][:3], ["docker", "network", "create"])
        run = runner_with(network_exists=True)
        bootstrap.ensure_network(run)
        self.assertEqual(len(run.calls), 1)

    def test_bootstrap_selects_s3_and_uses_suffix_without_rewriting_operator_lines(self):
        self.render()
        before = self.env.read_text().replace('OB_PUBLIC_PORT_SUFFIX=\n', 'OB_PUBLIC_PORT_SUFFIX=:8080\n')
        before += "MY_CUSTOM='value with spaces'\n"
        self.env.write_text(before)
        source = Path(bootstrap.__file__).resolve().parent.parent
        (self.root / 'compose.yaml').write_text((source / 'compose.yaml').read_text())
        run = runner_with()
        with patch.object(bootstrap, '__file__', str(self.root / 'scripts' / 'bootstrap.py')), patch.object(bootstrap, 'wait_ready') as ready, patch.dict(os.environ, {'COMPOSE_PROFILES': 's3'}):
            self.assertEqual(bootstrap.bootstrap(['--template', str(self.template)], runner=run), 0)
        after = self.env.read_text()
        self.assertIn('COMPOSE_FILE=compose.yaml:compose.s3.yaml\n', after)
        self.assertIn('COMPOSE_PROFILES=s3\n', after)
        self.assertIn('OB_PUBLIC_PORT_SUFFIX=:8080\n', after)
        for line in before.splitlines():
            if not line.startswith(('COMPOSE_FILE=', 'COMPOSE_PROFILES=', 'OB_PUBLIC_PORT_SUFFIX=')):
                self.assertIn(line + '\n', after)
        up = next(call for call in run.calls if call[:2] == ['docker', 'compose'])
        self.assertIn(str(self.root / 'compose.s3.yaml'), up)
        self.assertEqual([call.args[0] for call in ready.call_args_list],
                         ['http://127.0.0.1:80/health/' + name for name in ('grafana','loki','tempo','mimir','alloy')])

    def test_readiness_origin_uses_the_local_https_listener(self):
        self.render()
        text = self.env.read_text()
        text = text.replace("OB_PUBLIC_DOMAIN=localhost\n", "OB_PUBLIC_DOMAIN=observe.example.com\n")
        text = text.replace("OB_SCHEME=http\n", "OB_SCHEME=https\n")
        text = text.replace("OB_HTTPS_PORT=443\n", "OB_HTTPS_PORT=80\n")
        text = text.replace("OB_PUBLIC_PORT_SUFFIX=\n", "OB_PUBLIC_PORT_SUFFIX=:9443\n")
        self.env.write_text(text)
        source = Path(bootstrap.__file__).resolve().parent.parent
        (self.root / "compose.yaml").write_text((source / "compose.yaml").read_text())
        with patch.object(bootstrap, "__file__", str(self.root / "scripts" / "bootstrap.py")), patch.object(bootstrap, "wait_ready") as ready:
            self.assertEqual(bootstrap.bootstrap(["--template", str(self.template)], runner=runner_with()), 0)
        self.assertEqual(
            [call.args[0] for call in ready.call_args_list],
            ["https://127.0.0.1:80/health/" + name for name in ("grafana", "loki", "tempo", "mimir", "alloy")],
        )

    def test_bootstrap_refuses_a_storage_switch_before_compose(self):
        self.render()
        state = self.root / 'data' / 'installation'
        state.mkdir(parents=True)
        (state / 'storage-mode').write_text('s3\n')
        before = self.env.read_text()
        run = runner_with()
        with patch.object(bootstrap, '__file__', str(self.root / 'scripts' / 'bootstrap.py')):
            with self.assertRaises(bootstrap.Refused) as ctx:
                bootstrap.bootstrap(['--template', str(self.template)], runner=run)
        self.assertEqual(ctx.exception.code, 'storage_migration_required')
        self.assertFalse(any(call[:2] == ['docker','compose'] for call in run.calls))
        self.assertEqual(self.env.read_text(), before)

    def test_suffix_is_derived_for_the_public_scheme_once(self):
        for scheme, http, https, expected in [('http', '8080', '8443', ':8080'),
                                               ('https', '8080', '8443', ':8443'),
                                               ('http', '443', '80', ':443'),
                                               ('https', '443', '80', ':80')]:
            with self.subTest(scheme=scheme, http=http):
                self.env.write_text(f'OB_SCHEME={scheme}\nOB_HTTP_PORT={http}\nOB_HTTPS_PORT={https}\n')
                self.render()
                self.assertIn(f'OB_PUBLIC_PORT_SUFFIX={expected}\n', self.env.read_text())
                before = self.env.read_text()
                self.render()
                self.assertEqual(self.env.read_text(), before)
                self.assertEqual(before.count('OB_PUBLIC_PORT_SUFFIX='), 1)

    def test_existing_volumes_without_marker_refuse_with_all_secrets_present(self):
        self.render()
        before = self.env.read_text()
        run = runner_with(volumes=['legacy-loki'], labelled_volumes=['legacy-loki'])
        with patch.object(bootstrap, '__file__', str(self.root / 'scripts/bootstrap.py')):
            with self.assertRaises(bootstrap.Refused) as ctx:
                bootstrap.bootstrap(['--template', str(self.template)], runner=run)
        self.assertEqual(ctx.exception.code, 'storage_mode_unknown')
        self.assertFalse(any(call[:2] == ['docker', 'compose'] for call in run.calls))
        self.assertEqual(self.env.read_text(), before)

    def test_local_listener_probe_sends_public_host(self):
        settings = {'OB_SCHEME': 'https', 'OB_LISTEN_SCHEME': 'http',
                    'OB_HTTP_PORT': '18080', 'OB_PUBLIC_DOMAIN': 'observe.example.com'}
        with patch.object(bootstrap.urllib.request, 'urlopen') as opened:
            opened.return_value.__enter__.return_value.status = 200
            bootstrap.wait_ready(bootstrap.local_origin(settings) + '/health/grafana',
                                 host=settings['OB_PUBLIC_DOMAIN'])
        request = opened.call_args.args[0]
        self.assertEqual(request.full_url, 'http://127.0.0.1:18080/health/grafana')
        self.assertEqual(request.get_header('Host'), 'observe.example.com')
        settings['OB_LISTEN_SCHEME'] = 'https'
        settings['OB_HTTPS_PORT'] = '18443'
        with patch.object(bootstrap, 'LocalHTTPSConnection') as connection:
            connection.return_value.getresponse.return_value.status = 200
            bootstrap.wait_ready(bootstrap.local_origin(settings) + '/health/grafana',
                                 host=settings['OB_PUBLIC_DOMAIN'])
        connection.assert_called_once_with('observe.example.com', port=18443, timeout=5)
        connection.return_value.request.assert_called_once_with(
            'GET', '/health/grafana', headers={'Host': 'observe.example.com'})


    def test_suffix_not_derived_behind_edge(self):
        self.env.write_text('OB_SCHEME=https\nOB_LISTEN_SCHEME=http\nOB_HTTP_PORT=18180\nOB_HTTPS_PORT=18543\n')
        self.render()
        self.assertNotIn('OB_PUBLIC_PORT_SUFFIX=:', self.env.read_text())


if __name__ == "__main__":
    unittest.main()
