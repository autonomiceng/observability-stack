"""Bootstrap contract. Docker is never called; a fake runner answers instead."""

import io
import itertools
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

    def run(argv, **options):
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
        if argv[:2] == ["docker", "compose"] and "config" in argv:
            refs = bootstrap.images(Path(__file__).resolve().parent.parent / 'compose.yaml')
            return subprocess.CompletedProcess(argv, 0, json.dumps({
                'services': {name: {'image': ref} for name, ref in refs.items()}}), "")
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

    def test_complete_image_overrides_survive_render_with_empty_and_unset_defaults(self):
        compose = self.template.parent / 'compose.yaml'
        defaults = bootstrap.images(compose)
        for value in ('registry.example:5000/team/image:experiment', '', None):
            with self.subTest(value=value):
                lines = []
                for service, ref in defaults.items():
                    key = 'OB_' + service.removesuffix('-init').upper() + '_IMAGE'
                    self.assertIn('${' + key + ':-' + ref + '}', compose.read_text())
                    if service != 'rustfs-init' and value is not None:
                        lines.append(f'{key}={value}')
                self.env.write_text('\n'.join(lines) + '\n')
                self.render()
                for line in lines:
                    self.assertIn(line + '\n', self.env.read_text())
                self.assertEqual(bootstrap.images(compose), defaults)
        self.assertEqual(defaults['rustfs'], defaults['rustfs-init'])

    def test_default_validation_clears_shell_image_and_compose_overrides(self):
        script = self.template.parent / 'scripts/validate.sh'
        prefix = script.read_text().split('for tool in ')[0]
        poison = {'OB_CADDY_IMAGE': 'private-experiment', 'OB_LOKI_IMAGE': 'local-only',
                  'COMPOSE_FILE': 'unrelated.yaml', 'COMPOSE_ENV_FILES': 'private.env',
                  'COMPOSE_PROFILES': 's3'}
        with patch.dict(os.environ, poison):
            result = subprocess.run(['sh', '-c', prefix + '\nenv', str(script)],
                                    text=True, capture_output=True, check=True)
        names = {line.split('=', 1)[0] for line in result.stdout.splitlines()}
        self.assertFalse(names & poison.keys())

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
        bootstrap.write_versions(self.root, compose, {"OB_S3_SECRET_KEY": "never-publish"}, services={
            "grafana": {"image": "private.example/custom:grafana-test"},
            "loki": {"image": "user:password@registry/loki:secret"},
            "tempo": {"image": "grafana/tempo@sha256:" + "a" * 64},
            "unexpected": {"image": "secret"}})
        doc = json.loads((self.root / "data" / "console" / "versions.json").read_text())
        self.assertEqual(doc["images"]["grafana"], "grafana-test")
        self.assertEqual(doc["images"]["loki"], "unknown")
        self.assertEqual(doc["images"]["tempo"], "sha256:" + "a" * 64)
        for private in ("never-publish", "password", "unexpected", "private.example"):
            self.assertNotIn(private, json.dumps(doc))
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

    def test_docker_timeout_reports_failure_without_exposing_command_details(self):
        options = []
        def observe(argv, **kwargs):
            options.append(kwargs)
            return subprocess.CompletedProcess(argv, 0, '', '')
        with patch.object(bootstrap.subprocess, 'run', side_effect=observe):
            selected = bootstrap.bootstrap.__defaults__[0]
            selected(['docker', 'network', 'inspect', 'test'])
            bootstrap.compose_up(self.root, self.env, selected)
            selected(['python3', 'status_observer.py'], timeout=120)
        self.assertEqual([row['timeout'] for row in options], [60, 900, 120])
        with patch.object(bootstrap, 'bootstrap', side_effect=subprocess.TimeoutExpired(['private'], 60)), \
                patch('sys.stderr', new_callable=io.StringIO) as error:
            self.assertEqual(bootstrap.main(), 3)
            self.assertEqual(json.loads(error.getvalue())['error'], 'docker_timeout')
            self.assertNotIn('private', error.getvalue())

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
        up = next(call for call in run.calls if call[:2] == ['docker', 'compose'] and 'up' in call)
        self.assertIn(str(self.root / 'compose.s3.yaml'), up)
        self.assertEqual([call.args[0] for call in ready.call_args_list],
                         [origin + '/health/' + name for origin in ('http://127.0.0.1:80', 'https://127.0.0.1:443')
                          for name in ('grafana','loki','tempo','mimir','alloy')])

    def test_readiness_origin_uses_the_local_https_listener(self):
        self.render()
        text = self.env.read_text()
        text = text.replace("OB_PUBLIC_DOMAIN=localhost\n", "OB_PUBLIC_DOMAIN=observe.example.com\n")
        text = text.replace("OB_SCHEME=http\n", "OB_SCHEME=https\n")
        text = text.replace("OB_HTTPS_PORT=443\n", "OB_HTTPS_PORT=18443\n")
        text = text.replace("OB_PUBLIC_PORT_SUFFIX=\n", "OB_PUBLIC_PORT_SUFFIX=:9443\n")
        self.env.write_text(text)
        source = Path(bootstrap.__file__).resolve().parent.parent
        (self.root / "compose.yaml").write_text((source / "compose.yaml").read_text())
        with patch.object(bootstrap, "__file__", str(self.root / "scripts" / "bootstrap.py")), patch.object(bootstrap, "wait_ready") as ready:
            self.assertEqual(bootstrap.bootstrap(["--template", str(self.template)], runner=runner_with()), 0)
        self.assertEqual(
            [call.args[0] for call in ready.call_args_list],
            [origin + "/health/" + name for origin in ("http://127.0.0.1:80", "https://127.0.0.1:18443")
             for name in ("grafana", "loki", "tempo", "mimir", "alloy")],
        )
        self.assertTrue(all("ca_data" in call.kwargs for call in ready.call_args_list[5:]))

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
                self.env.write_text(f'OB_ACCESS_MODE=local\nOB_SCHEME={scheme}\nOB_HTTP_PORT={http}\nOB_HTTPS_PORT={https}\n')
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
        settings = {'OB_SCHEME': 'https', 'OB_ACCESS_MODE': 'proxy',
                    'OB_HTTP_PORT': '18080', 'OB_PUBLIC_DOMAIN': 'observe.example.com'}
        with patch.object(bootstrap.urllib.request, 'urlopen') as opened:
            opened.return_value.__enter__.return_value.status = 200
            bootstrap.wait_ready(bootstrap.local_origin(settings) + '/health/grafana',
                                 host=settings['OB_PUBLIC_DOMAIN'])
        request = opened.call_args.args[0]
        self.assertEqual(request.full_url, 'http://127.0.0.1:18080/health/grafana')
        self.assertEqual(request.get_header('Host'), 'observe.example.com')
        settings['OB_ACCESS_MODE'] = 'public'
        settings['OB_HTTPS_PORT'] = '18443'
        with patch.object(bootstrap, 'LocalHTTPSConnection') as connection:
            connection.return_value.getresponse.return_value.status = 200
            bootstrap.wait_ready(bootstrap.local_origin(settings) + '/health/grafana',
                                 host=settings['OB_PUBLIC_DOMAIN'])
        connection.assert_called_once_with('observe.example.com', port=18443, timeout=5)
        connection.return_value.request.assert_called_once_with(
            'GET', '/health/grafana', headers={'Host': 'observe.example.com'})


    def test_suffix_not_derived_behind_edge(self):
        self.env.write_text('OB_ACCESS_MODE=proxy\nOB_TRUSTED_PROXIES=192.0.2.2\nOB_SCHEME=https\nOB_HTTP_PORT=18180\nOB_HTTPS_PORT=18543\n')
        self.render()
        self.assertNotIn('OB_PUBLIC_PORT_SUFFIX=:', self.env.read_text())

    def test_access_defaults_and_invalid_configuration(self):
        for mode, expected in [("local", "http"), ("public", "https"), ("proxy", "https")]:
            settings = {"OB_ACCESS_MODE": mode, "OB_PUBLIC_DOMAIN": "observe.example.com",
                        "OB_TRUSTED_PROXIES": "192.0.2.2/32"}
            bootstrap.access_config(settings)
            self.assertEqual(settings["OB_SCHEME"], expected)
        settings = {}
        bootstrap.access_config(settings)
        self.assertEqual(settings["OB_ACCESS_MODE"], "local")
        for invalid in ({"OB_ACCESS_MODE": "unknown"}, {"OB_SCHEME": "ftp"},
                        {"OB_ACCESS_MODE": "public", "OB_SCHEME": "http"},
                        {"OB_ACCESS_MODE": "proxy"}, {"OB_TRUSTED_PROXIES": "172.16.0.0/12"},
                        {"OB_PUBLIC_DOMAIN": "localhost {"}, {"OB_PUBLIC_PORT_SUFFIX": ":70000"}):
            with self.subTest(invalid=invalid), self.assertRaises(bootstrap.Refused):
                bootstrap.access_config(invalid.copy())

    def test_access_rejects_invalid_dns_labels_in_either_host(self):
        for key in ("OB_PUBLIC_DOMAIN", "OB_GRAFANA_HOST"):
            for host in ("foo.-bar.example.com", "foo.bar-.example.com",
                         "a" * 64 + ".example.com", "foo..example.com",
                         ".example.com", "example.com.", "foo._bar.example.com"):
                with self.subTest(key=key, host=host):
                    settings = {"OB_ACCESS_MODE": "public", "OB_PUBLIC_DOMAIN": "observe.example.com",
                                "OB_GRAFANA_HOST": "grafana.example.com", key: host}
                    with self.assertRaises(bootstrap.Refused) as ctx:
                        bootstrap.access_config(settings)
                    self.assertEqual(ctx.exception.code, "access_host_invalid")

    def test_access_enforces_complete_hostname_length(self):
        for key in ("OB_PUBLIC_DOMAIN", "OB_GRAFANA_HOST", "OB_RUSTFS_HOST"):
            for length in (253, 254):
                host = ".".join(["a" * 63] * 3 + ["b" * (length - 192)])
                settings = {"OB_ACCESS_MODE": "public", "OB_PUBLIC_DOMAIN": "observe.example.com",
                            "OB_GRAFANA_HOST": "grafana.example.com", "OB_RUSTFS_HOST": "rustfs.example.com", key: host}
                with self.subTest(key=key, length=length):
                    if length == 253:
                        bootstrap.access_config(settings)
                    else:
                        with self.assertRaises(bootstrap.Refused) as ctx:
                            bootstrap.access_config(settings)
                        self.assertEqual(ctx.exception.code, "access_host_invalid")

    def test_access_accepts_valid_dns_label_boundaries(self):
        for host in ("a.example.com", "a" * 63 + ".example.com", "Foo.b-ar.example.com"):
            with self.subTest(host=host):
                settings = {"OB_ACCESS_MODE": "public", "OB_PUBLIC_DOMAIN": host}
                bootstrap.access_config(settings)
                self.assertEqual(settings["OB_GRAFANA_HOST"], "grafana." + host)

    def test_shell_access_settings_survive_restart_without_changing_other_lines(self):
        for mode in ("local", "public", "proxy"):
            with self.subTest(mode=mode), patch.dict(os.environ, {"OB_ALERTS": "placeholder"}, clear=True):
                self.env.unlink(missing_ok=True)
                self.render()
                extra = "# operator comment\nMY_CUSTOM='value with spaces'\nOB_BACKPLANE_OPERATIONS_TOKEN=existing-token\n"
                self.env.write_text(self.env.read_text() + extra)
                before_lines, before_secrets = bootstrap.read_env(self.env)
                overrides = {
                    "OB_ACCESS_MODE": mode, "OB_PUBLIC_DOMAIN": "observe.example.com",
                    "OB_GRAFANA_HOST": "dash.example.com", "OB_SCHEME": "https",
                    "OB_BIND_HOST": "0.0.0.0", "OB_HTTP_PORT": "18080", "OB_HTTPS_PORT": "18443",
                    "OB_PUBLIC_PORT_SUFFIX": ":9443", "OB_TRUSTED_PROXIES": "192.0.2.2/32 2001:db8::2/128",
                    "OB_OPERATOR_ALLOW": "192.0.2.3/32 ::1",
                }
                with patch.dict(os.environ, overrides):
                    self.assertEqual(self.render(), 0)
                saved = self.env.read_text()
                for key, value in overrides.items():
                    self.assertTrue(f"{key}={value}\n" in saved, key)
                self.assertEqual(bootstrap.read_env(self.env)[1], before_secrets)
                for line in before_lines:
                    match = bootstrap.ENV_LINE.match(line)
                    if not match or match.group("key") not in {*overrides, "COMPOSE_FILE"}:
                        self.assertIn(line + "\n", saved)
                self.assertEqual(self.render(), 0)
                self.assertEqual(self.env.read_text(), saved)

    def test_public_bootstrap_requires_grafana_https_before_reporting_ready(self):
        source = Path(bootstrap.__file__).resolve().parent.parent
        shutil.copyfile(source / "compose.yaml", self.root / "compose.yaml")
        for tls_fails in (False, True):
            with self.subTest(tls_fails=tls_fails):
                self.env.write_text("OB_ACCESS_MODE=public\nOB_PUBLIC_DOMAIN=observe.example.com\n"
                                    "OB_GRAFANA_HOST=dash.example.com\nOB_HTTPS_PORT=18443\n"
                                    "OB_PUBLIC_PORT_SUFFIX=:9443\n")
                self.render()
                output = io.StringIO()
                with patch.object(bootstrap, "__file__", str(self.root / "scripts/bootstrap.py")), \
                        patch.object(bootstrap, "LocalHTTPSConnection") as connection, \
                        patch.object(bootstrap.time, "sleep"), \
                        patch.object(bootstrap.time, "monotonic", side_effect=itertools.count()), \
                        patch("sys.stdout", output), patch.object(bootstrap.shutil, "which", return_value="docker"):
                    connection.return_value.getresponse.return_value.status = 200

                    def request(method, path, headers):
                        if tls_fails and headers["Host"] == "dash.example.com":
                            raise bootstrap.ssl.SSLCertVerificationError("Grafana certificate unavailable")

                    connection.return_value.request.side_effect = request
                    if tls_fails:
                        with self.assertRaises(bootstrap.Refused) as ctx:
                            bootstrap.bootstrap(["--template", str(self.template)], runner=runner_with())
                        self.assertEqual(ctx.exception.code, "not_ready")
                        self.assertEqual(output.getvalue(), "")
                    else:
                        self.assertEqual(bootstrap.bootstrap(["--template", str(self.template)], runner=runner_with()), 0)
                        self.assertEqual(json.loads(output.getvalue())["grafana"], "https://dash.example.com:9443/")
                    self.assertEqual([call.args[0] for call in connection.call_args_list[:5]],
                                     ["observe.example.com"] * 5)
                    connection.assert_called_with("dash.example.com", port=18443, timeout=5)
                    connection.return_value.request.assert_called_with("GET", "/login", headers={"Host": "dash.example.com"})

    def test_public_https_connection_dials_loopback_with_verified_sni(self):
        with patch.object(bootstrap.socket, "create_connection") as dial:
            connection = bootstrap.LocalHTTPSConnection("dash.example.com", port=18443, timeout=5)
            self.assertTrue(connection._context.check_hostname)
            self.assertEqual(connection._context.verify_mode, bootstrap.ssl.CERT_REQUIRED)
            with patch.object(connection._context, "wrap_socket") as wrap:
                connection.connect()
            dial.assert_called_once_with(("127.0.0.1", 18443), timeout=5)
            wrap.assert_called_once_with(dial.return_value, server_hostname="dash.example.com")

    def test_ip_root_keeps_a_configured_grafana_origin(self):
        settings = {"OB_PUBLIC_DOMAIN": "127.0.0.1", "OB_STATE_DIR": str(self.root),
                    "OB_PUBLIC_PORT_SUFFIX": ":18080"}
        bootstrap.access_config(settings)
        self.assertEqual(settings["OB_GRAFANA_HOST"], "grafana.localhost")
        bootstrap.write_versions(self.root, self.template.parent / "compose.yaml", settings, services={})
        self.assertEqual(json.loads((self.root / "console/links.json").read_text()),
                         {"grafana": "http://grafana.localhost:18080"})
        settings["OB_ACCESS_MODE"] = "public"
        settings["OB_SCHEME"] = "https"
        with self.assertRaises(bootstrap.Refused):
            bootstrap.access_config(settings)

    def test_local_https_readiness_verifies_with_own_public_ca(self):
        with patch.object(bootstrap.ssl, "create_default_context") as context, patch.object(bootstrap, "LocalHTTPSConnection") as connection:
            connection.return_value.getresponse.return_value.status = 200
            bootstrap.wait_ready("https://127.0.0.1:18443/health/grafana", host="localhost", ca_data="public-root")
        context.assert_called_once_with(cadata="public-root")
        connection.assert_called_once_with("localhost", port=18443, timeout=5, context=context.return_value)

    def test_grafana_url_explicit_default_and_invalid_origins(self):
        for mode in ("local", "public", "proxy"):
            settings = {"OB_ACCESS_MODE": mode, "OB_PUBLIC_DOMAIN": "observe.example.com",
                        "OB_TRUSTED_PROXIES": "192.0.2.2/32", "OB_PUBLIC_PORT_SUFFIX": ":9443"}
            bootstrap.access_config(settings)
            scheme = "http" if mode == "local" else "https"
            self.assertEqual(bootstrap.grafana_origin(settings), scheme + "://grafana.observe.example.com:9443")
            settings["OB_GRAFANA_URL"] = "https://darkforge.tail694fe2.ts.net:8447"
            bootstrap.access_config(settings)
            self.assertEqual(bootstrap.grafana_origin(settings), settings["OB_GRAFANA_URL"])
            self.assertEqual(settings["OB_GRAFANA_URL_HOST"], "darkforge.tail694fe2.ts.net")
            self.assertEqual(settings["OB_GRAFANA_AUTHORITY"], "darkforge.tail694fe2.ts.net:8447")
            self.assertEqual(settings["OB_GRAFANA_HOST"], "grafana.observe.example.com")
            settings["OB_GRAFANA_URL"] = ""
            bootstrap.access_config(settings)
            self.assertEqual(settings["OB_GRAFANA_AUTHORITY"], "")
            self.assertEqual(settings["OB_GRAFANA_URL_HOST"], "")
        for origin in ("https://localhost", "http://127.0.0.1:8080", "https://[::1]:8447"):
            bootstrap.access_config({"OB_GRAFANA_URL": origin})
        for origin in ("ftp://host", "//host:8447", "https://", "https://host/", "https://host/path",
                       "https://user:password@host", "https://host?", "https://host#", "https://host?q=x",
                       "https://host#fragment", "https://host:0", "https://host:65536", "https://host:abc",
                       "https://host:", "https://host:08447", "https://bad_host", "https://-host",
                       "https://host..name", "https://host\\path", " https://host", "https://ho\nst",
                       "https://host'", "https://{host}"):
            with self.subTest(origin=origin), self.assertRaises(bootstrap.Refused) as refused:
                bootstrap.access_config({"OB_GRAFANA_URL": origin})
            self.assertEqual(refused.exception.code, "grafana_url_invalid")

    def test_grafana_default_ports_are_canonicalized(self):
        for scheme, port in (("http", 80), ("https", 443)):
            for host in ("grafana.example.com", "[::1]"):
                with self.subTest(scheme=scheme, host=host):
                    settings = {"OB_ACCESS_MODE": "proxy", "OB_TRUSTED_PROXIES": "192.0.2.2",
                                "OB_GRAFANA_URL": f"{scheme}://{host}:{port}"}
                    bootstrap.access_config(settings)
                    self.assertEqual(settings["OB_GRAFANA_URL"], f"{scheme}://{host}")
                    self.assertEqual(settings["OB_GRAFANA_AUTHORITY"], host)
                    self.assertEqual(bootstrap.grafana_origin(settings), f"{scheme}://{host}")
                    settings["OB_GRAFANA_URL"] = f"{scheme}://{host}:8447"
                    bootstrap.access_config(settings)
                    self.assertEqual(settings["OB_GRAFANA_URL"], f"{scheme}://{host}:8447")
                    self.assertEqual(settings["OB_GRAFANA_AUTHORITY"], f"{host}:8447")

    def test_generated_grafana_origin_and_console_links(self):
        source = self.template.parent
        (self.root / "compose.yaml").write_text((source / "compose.yaml").read_text())
        origin = "https://darkforge.tail694fe2.ts.net:8447"
        for siblings in (False, True):
            _, existing = bootstrap.read_env(self.env)
            retained = "".join(f"{key}={value}\n" for key, value in existing.items())
            self.env.write_text(retained + "OB_ACCESS_MODE=proxy\nOB_TRUSTED_PROXIES=192.0.2.2/32\n"
                                "OB_GRAFANA_URL=" + origin + "\n" +
                                ("OB_GATEWAY_URL=https://darkforge.tail694fe2.ts.net:8443\n"
                                 "OB_BACKPLANE_URL=https://darkforge.tail694fe2.ts.net:8445\n" if siblings else ""))
            with patch.object(bootstrap, "__file__", str(self.root / "scripts/bootstrap.py")), \
                 patch.object(bootstrap, "wait_ready"):
                self.assertEqual(bootstrap.bootstrap(["--template", str(self.template)], runner=runner_with()), 0)
            lines, _ = bootstrap.read_env(self.env)
            settings = {m['key']: bootstrap.unquote(m['value']) for m in map(bootstrap.ENV_LINE.match, lines) if m}
            self.assertEqual(settings["OB_GRAFANA_URL"], origin)
            self.assertEqual(settings["OB_GRAFANA_URL_HOST"], "darkforge.tail694fe2.ts.net")
            self.assertEqual(settings["OB_GRAFANA_AUTHORITY"], "darkforge.tail694fe2.ts.net:8447")
            expected = {"grafana": origin}
            if siblings:
                expected.update(gateway=settings["OB_GATEWAY_URL"], backplane=settings["OB_BACKPLANE_URL"])
            self.assertEqual(json.loads((self.root / "data/console/links.json").read_text()), expected)
            self.assertEqual(settings["OB_TRUSTED_PROXIES"], "192.0.2.2/32")

    def test_rustfs_console_off_on_s3_and_filesystem_refusal(self):
        for profile in ("", "s3"):
            settings = {"COMPOSE_PROFILES": profile, "OB_STATE_DIR": str(self.root)}
            bootstrap.access_config(settings)
            self.assertEqual(settings["OB_RUSTFS_CONSOLE"], "false")
            bootstrap.write_versions(self.root, self.template.parent / "compose.yaml", settings, services={})
            self.assertNotIn("rustfs", json.loads((self.root / "console/links.json").read_text()))
        settings["OB_RUSTFS_CONSOLE"] = "true"
        bootstrap.access_config(settings)
        self.assertEqual(bootstrap.rustfs_origin(settings), "http://rustfs.localhost")
        bootstrap.write_versions(self.root, self.template.parent / "compose.yaml", settings, services={})
        self.assertEqual(json.loads((self.root / "console/links.json").read_text())["rustfs"],
                         "http://rustfs.localhost")
        self.env.write_text("OB_RUSTFS_CONSOLE=true\n")
        before = self.env.read_bytes()
        with self.assertRaises(bootstrap.Refused) as refused:
            self.render()
        self.assertEqual(refused.exception.code, "rustfs_console_requires_s3")
        self.assertEqual(self.env.read_bytes(), before)
        with self.assertRaises(bootstrap.Refused) as refused:
            bootstrap.access_config({"OB_RUSTFS_CONSOLE": "yes"})
        self.assertEqual(refused.exception.code, "rustfs_console_invalid")

        # Even disabled origins reserve a Caddy site: validate derived names before writing.
        derived_limit = ".".join(["a" * 63] * 3 + ["b" * 54])
        valid = {"OB_PUBLIC_DOMAIN": derived_limit, "OB_GRAFANA_HOST": "grafana.example.com"}
        bootstrap.access_config(valid)
        self.assertEqual(len(valid["OB_RUSTFS_HOST"]), 253)
        for overrides in ({"OB_PUBLIC_DOMAIN": derived_limit + "b", "OB_GRAFANA_HOST": "grafana.example.com"},
                          {"OB_PUBLIC_DOMAIN": "example.com", "OB_GRAFANA_HOST": "rustfs.example.com",
                           "OB_GRAFANA_URL": "https://machine.example.com:8447"}):
            self.env.write_text("".join(f"{key}={value}\n" for key, value in overrides.items()))
            before = self.env.read_bytes()
            with self.assertRaises(bootstrap.Refused) as refused:
                self.render()
            self.assertEqual(refused.exception.code, "access_host_invalid")
            self.assertEqual(self.env.read_bytes(), before)

    def test_rustfs_full_authority_and_untrusted_configuration(self):
        settings = {"OB_ACCESS_MODE": "proxy", "OB_TRUSTED_PROXIES": "192.0.2.2/32",
                    "OB_PUBLIC_DOMAIN": "machine.example.com", "OB_PUBLIC_PORT_SUFFIX": ":8446",
                    "OB_GRAFANA_URL": "https://machine.example.com:8447",
                    "OB_RUSTFS_URL": "https://machine.example.com:8451"}
        bootstrap.access_config(settings)
        self.assertEqual(settings["OB_RUSTFS_AUTHORITY"], "machine.example.com:8451")
        self.assertEqual(settings["OB_GRAFANA_AUTHORITY"], "machine.example.com:8447")
        self.assertEqual(settings["OB_RUSTFS_URL_HOST"], "machine.example.com")
        for value in ("https://user:secret@machine.example.com:8451", "https://machine.example.com/path",
                      "https://machine.example.com:8451'", "https://machine.example.com\n"):
            with self.assertRaises(bootstrap.Refused) as refused:
                bootstrap.access_config(settings | {"OB_RUSTFS_URL": value})
            self.assertEqual(refused.exception.code, "rustfs_url_invalid")
        for value in (settings["OB_GRAFANA_URL"], "https://machine.example.com:8446"):
            with self.assertRaises(bootstrap.Refused) as refused:
                bootstrap.access_config(settings | {"OB_RUSTFS_URL": value})
            self.assertEqual(refused.exception.code, "rustfs_origin_conflict")
        # Cross-app aliases collide even at different ports and with the console off.
        for overrides in ({"OB_GRAFANA_URL": "", "OB_GRAFANA_HOST": "Machine.Example.com"},
                          {"OB_RUSTFS_URL": "", "OB_RUSTFS_HOST": "Machine.Example.com"}):
            with self.assertRaises(bootstrap.Refused) as refused:
                bootstrap.access_config(settings | {"OB_PUBLIC_DOMAIN": "observe.example.com"} | overrides)
            self.assertEqual(refused.exception.code, "rustfs_origin_conflict")
        with self.assertRaises(bootstrap.Refused) as refused:
            bootstrap.access_config(settings | {"OB_PUBLIC_PORT_SUFFIX": ":443",
                                                "OB_RUSTFS_URL": "https://machine.example.com:443"})
        self.assertEqual(refused.exception.code, "rustfs_origin_conflict")
        with self.assertRaises(bootstrap.Refused) as refused:
            bootstrap.access_config(settings | {"OB_TRUSTED_PROXIES": "100.64.0.0/10"})
        self.assertEqual(refused.exception.code, "proxy_trust_invalid")
        bootstrap.access_config(settings | {"OB_TRUSTED_PROXIES": "100.100.1.2/32 fd7a:115c:a1e0::1/128"})
        canonical = settings | {"OB_RUSTFS_URL": "https://[::1]:443"}
        bootstrap.access_config(canonical)
        self.assertEqual(canonical["OB_RUSTFS_AUTHORITY"], "[::1]")

    def test_rustfs_console_preserves_existing_s3_origins_and_secret_custody(self):
        shutil.copy(self.template.parent / "compose.yaml", self.root / "compose.yaml")
        self.env.write_text("COMPOSE_PROFILES=s3\nOB_ACCESS_MODE=proxy\n"
                            "OB_TRUSTED_PROXIES=192.0.2.2/32\n"
                            "OB_GRAFANA_URL=https://machine.example.com:8447\n"
                            "OB_RUSTFS_URL=https://machine.example.com:8451\n"
                            "OB_RUSTFS_IMAGE=local-experiment:dev\n")
        self.render()
        _, secrets_before = bootstrap.read_env(self.env)
        marker = self.root / "data/installation/storage-mode"
        marker.parent.mkdir(parents=True)
        marker.write_text("s3\n")
        self.env.write_text(self.env.read_text().replace("OB_RUSTFS_CONSOLE=false", "OB_RUSTFS_CONSOLE=true"))
        with patch.object(bootstrap, "__file__", str(self.root / "scripts/bootstrap.py")), \
             patch.object(bootstrap, "wait_ready"):
            self.assertEqual(bootstrap.bootstrap(["--template", str(self.template)], runner=runner_with()), 0)
        _, secrets_after = bootstrap.read_env(self.env)
        self.assertEqual(secrets_before, secrets_after)
        self.assertEqual(self.env.stat().st_mode & 0o777, 0o600)
        self.assertEqual(marker.read_text(), "s3\n")
        self.assertIn("OB_RUSTFS_IMAGE=local-experiment:dev\n", self.env.read_text())
        links = (self.root / "data/console/links.json").read_text()
        self.assertEqual(json.loads(links), {"grafana": "https://machine.example.com:8447",
                                           "rustfs": "https://machine.example.com:8451"})
        for value in secrets_before.values():
            self.assertNotIn(value, links)

    def test_proxy_records_override_and_starts_with_it_in_both_storage_modes(self):
        for profile in ("", "s3"):
            self.env.write_text("OB_ACCESS_MODE=proxy\nOB_SCHEME=https\nOB_TRUSTED_PROXIES=192.0.2.2\nCOMPOSE_PROFILES=" + profile + "\n")
            self.render()
            expected = "compose.yaml:" + ("compose.s3.yaml:" if profile else "") + "compose.proxy.yaml"
            self.assertIn("COMPOSE_FILE=" + expected + "\n", self.env.read_text())
            run = runner_with()
            bootstrap.compose_up(self.root, self.env, run, bool(profile), True)
            self.assertIn(str(self.root / "compose.proxy.yaml"), run.calls[-1])


if __name__ == "__main__":
    unittest.main()

class ConsoleAccessTests(unittest.TestCase):
    def test_console_allowlist_is_independent_of_monitoring_operators(self):
        settings = {'COMPOSE_PROFILES': 's3', 'OB_RUSTFS_CONSOLE': 'true',
                    'OB_OPERATOR_ALLOW': '192.0.2.7/32', 'OB_RUSTFS_CONSOLE_ALLOW': '100.64.0.9/32'}
        bootstrap.access_config(settings)
        self.assertEqual(settings['OB_OPERATOR_ALLOW'], '192.0.2.7/32')
        self.assertEqual(settings['OB_RUSTFS_CONSOLE_ALLOW'], '100.64.0.9/32')
        default = {'OB_OPERATOR_ALLOW': '192.0.2.7/32'}
        bootstrap.access_config(default)
        self.assertEqual(default['OB_RUSTFS_CONSOLE_ALLOW'], '127.0.0.1/8 ::1')
        for value in ('', 'private_ranges', 'example.com', '127.0.0.1 {', '192.0.2.1/33'):
            with self.assertRaises(bootstrap.Refused):
                bootstrap.access_config(settings | {'OB_RUSTFS_CONSOLE_ALLOW': value})
