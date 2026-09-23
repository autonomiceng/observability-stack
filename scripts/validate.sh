#!/bin/sh
# Static gates and image-provided config validators. No stack is started.
set -eu
root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
cd "$root"
# Shipped-default gates ignore installation and caller overrides.
unset COMPOSE_FILE COMPOSE_PROFILES COMPOSE_ENV_FILES
for key in $(env | sed -n 's/^\(OB_[A-Z0-9_]*\)=.*/\1/p'); do
  unset "$key"
done
for tool in docker python3 shellcheck; do
  command -v "$tool" >/dev/null || { echo "missing tool: $tool" >&2; exit 1; }
done
shellcheck scripts/*.sh docker/caddy/*.sh
echo 'shellcheck: PASS'
python3 -m py_compile scripts/*.py tests/*.py
echo 'python: PASS'
docker compose version >/dev/null
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT HUP INT TERM
OB_ALERTS=placeholder python3 scripts/bootstrap.py --env-file "$work/.env" --render-only >/dev/null
OB_ALERTS=placeholder OB_ACCESS_MODE=proxy OB_TRUSTED_PROXIES=192.0.2.2/32 \
  OB_GRAFANA_URL=https://darkforge.tail694fe2.ts.net:8447 \
  OB_RUSTFS_URL=https://darkforge.tail694fe2.ts.net:8451 \
  python3 scripts/bootstrap.py --env-file "$work/url.env" --render-only >/dev/null
echo 'env render: PASS'
for mode in filesystem s3 proxy proxy-s3 proxy-url proxy-url-s3; do
  if [ "$mode" = proxy-url-s3 ]; then
    OB_RUSTFS_CONSOLE=true docker compose --env-file "$work/url.env" -f compose.yaml -f compose.s3.yaml -f compose.proxy.yaml --profile s3 config --format json > "$work/$mode.json"
  elif [ "$mode" = proxy-url ]; then
    docker compose --env-file "$work/url.env" -f compose.yaml -f compose.proxy.yaml config --format json > "$work/$mode.json"
  elif [ "$mode" = proxy-s3 ]; then
    docker compose --env-file "$work/.env" -f compose.yaml -f compose.s3.yaml -f compose.proxy.yaml --profile s3 config --format json > "$work/$mode.json"
  elif [ "$mode" = proxy ]; then
    docker compose --env-file "$work/.env" -f compose.yaml -f compose.proxy.yaml config --format json > "$work/$mode.json"
  elif [ "$mode" = s3 ]; then
    docker compose --env-file "$work/.env" -f compose.yaml -f compose.s3.yaml --profile s3 config --format json > "$work/$mode.json"
  else
    docker compose --env-file "$work/.env" -f compose.yaml config --format json > "$work/$mode.json"
  fi
done
python3 - "$work" <<'PY'
import json, re, sys
from pathlib import Path
for path in sorted(Path(sys.argv[1]).glob('*.json')):
    config = json.loads(path.read_text())
    services = config['services']
    published = {name for name, svc in services.items() if svc.get('ports')}
    assert published == {'caddy'}, f'only caddy publishes ports: {published}'
    shared = {name for name, svc in services.items() if 'platform' in svc.get('networks', {})}
    assert shared == {'caddy', 'grafana', 'alloy'}, f'platform members: {shared}'
    assert all(volume.get('external') for volume in config['volumes'].values())
    assert 'ob-alloy' in services['alloy']['networks']['platform']['aliases']
    assert 'ob-alloy-otlp' in services['alloy']['networks']['default']['aliases']
    assert {port['target'] for port in services['caddy']['ports']} == ({80} if path.stem.startswith('proxy') else {80, 443})
    assert services['grafana']['environment']['GF_LOG_MODE'] == 'console'
    explicit_url = path.stem.startswith('proxy-url')
    assert services['grafana']['environment']['GF_SERVER_ROOT_URL'] == (
        'https://darkforge.tail694fe2.ts.net:8447/' if explicit_url else 'http://grafana.localhost/')
    assert services['grafana']['environment']['GF_SERVER_DOMAIN'] == (
        'darkforge.tail694fe2.ts.net' if explicit_url else 'grafana.localhost')
    assert services['caddy']['environment']['OB_GRAFANA_AUTHORITY'] == (
        'darkforge.tail694fe2.ts.net:8447' if explicit_url else '')
    assert services['alloy']['environment']['OB_SCRAPE_EDGE'] == 'false'
    assert services['alloy']['environment']['OB_SCRAPE_GATEWAY'] == 'false'
    assert services['alloy']['environment']['OB_SCRAPE_BACKPLANE'] == 'false'
    unpinned = []
    for name, svc in services.items():
        ref = svc.get('image', '')
        if not re.fullmatch(r'.+:[^:@]+@sha256:[0-9a-f]{64}', ref) or ':latest@' in ref:
            unpinned.append(f'{name}: {ref}')
        if svc.get('restart') == 'unless-stopped':
            assert 0 < int(svc['mem_reservation']) < int(svc['mem_limit']), name
        assert not svc.get('env_file'), f'{name}: list environment explicitly'
        assert svc['logging'] == {'driver': 'journald', 'options': {'cache-disabled': 'true'}}, name
    if unpinned:
        sys.exit('unresolved image digests (resolve with docker buildx imagetools inspect):\n' + '\n'.join(unpinned))
    expected = {'caddy','grafana','alloy','loki','tempo','mimir'}
    if path.stem.endswith('s3'):
        expected |= {'rustfs','rustfs-init'}
        for name in ('loki','tempo','mimir'):
            assert any(v.get('source', '').endswith('/s3.yaml') for v in services[name]['volumes']), name
        assert services['rustfs']['environment']['RUSTFS_OBS_LOG_DIRECTORY'] == ''
        enabled = 'true' if path.stem == 'proxy-url-s3' else 'false'
        assert services['rustfs']['environment']['RUSTFS_CONSOLE_ENABLE'] == enabled
        assert services['rustfs']['environment']['RUSTFS_CONSOLE_ADDRESS'] == ':9001'
        assert services['caddy']['environment']['OB_RUSTFS_CONSOLE'] == enabled
    assert set(services) == expected, f'{path}: unexpected services'
    assert services['tempo'].get('stop_grace_period') == '45s', 'Tempo stop grace'
PY
python3 - <<'PY_IMAGES'
import re
from pathlib import Path
lines = [line.strip() for line in Path('compose.yaml').read_text().splitlines() if line.strip().startswith('image:')]
assert len(lines) == 8
assert all(re.fullmatch(r'image: \$\{OB_[A-Z0-9_]+_IMAGE:-[^\s{}]+:[^\s:@]+@sha256:[0-9a-f]{64}\}', line) for line in lines), 'Renovate-readable image defaults'
PY_IMAGES
echo 'compose config and pins: PASS'
# Alloy's unauthenticated UI and API show target labels; only bearer_token is redacted.
OB_SCRAPE_BACKPLANE=true OB_BACKPLANE_OPERATIONS_TOKEN=validation-only-backplane-token \
  docker compose --env-file "$work/.env" -f compose.yaml config --format json > "$work/token.out"
python3 - "$work/token.out" <<'PY'
import json, sys
from pathlib import Path
token = 'validation-only-backplane-token'
config = json.loads(Path(sys.argv[1]).read_text())
alloy = config['services']['alloy']
assert alloy['environment']['OB_SCRAPE_BACKPLANE'] == 'true'
assert alloy['environment']['OB_BACKPLANE_OPERATIONS_TOKEN'] == token
assert json.dumps(config).count(token) == 1, 'backplane token outside Alloy environment'
uses = [line.strip() for line in Path('config.alloy').read_text().splitlines() if 'OB_BACKPLANE_OPERATIONS_TOKEN' in line]
assert uses == ['bearer_token = sys.env("OB_BACKPLANE_OPERATIONS_TOKEN")'], uses
PY
echo 'backplane token confined to Alloy environment and bearer_token: PASS'
python3 - "$work" <<'PY'
import json, subprocess, sys
from pathlib import Path
work = Path(sys.argv[1])
original = (work / '.env').read_text()
for mode in ('filesystem', 's3'):
    defaults = {name: service['image'] for name, service in
                json.loads((work / (mode + '.json')).read_text())['services'].items()}
    command = ['docker', 'compose', '--env-file', str(work / 'images.env'), '-f', 'compose.yaml']
    if mode == 's3':
        command += ['-f', 'compose.s3.yaml', '--profile', 's3']
    for value in ('registry.example:5000/team/image:trial', 'local-experiment:dev', ''):
        keys = {'OB_' + name.removesuffix('-init').upper() + '_IMAGE' for name in defaults}
        (work / 'images.env').write_text(original + '\n' + ''.join(key + '=' + value + '\n' for key in sorted(keys)))
        result = subprocess.run(command + ['config', '--format', 'json'], capture_output=True, text=True)
        assert result.returncode == 0, 'image override configuration failed'
        actual = {name: service['image'] for name, service in json.loads(result.stdout)['services'].items()}
        assert actual == {name: value or ref for name, ref in defaults.items()}, (mode, value)
print('native image overrides: PASS (6 filesystem/S3 cases)')
PY
caddy_image=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["services"]["caddy"]["image"])' "$work/filesystem.json")
alloy_image=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["services"]["alloy"]["image"])' "$work/filesystem.json")
for enabled in false true; do
for mode in 'local localhost' 'local 127.0.0.1' 'public observe.example.com' 'proxy observe.example.com' 'proxy darkforge.tail694fe2.ts.net'; do
  # shellcheck disable=SC2086
  set -- $mode
  url_host=
  authority=
  rustfs_authority=
  if [ "$2" = darkforge.tail694fe2.ts.net ]; then
    url_host=$2
    authority=$2:8447
    rustfs_authority=$2:8451
  fi
  docker run --rm --log-driver=journald --log-opt cache-disabled=true \
    -e "OB_ACCESS_MODE=$1" -e "OB_PUBLIC_DOMAIN=$2" -e OB_GRAFANA_HOST=grafana.example.com \
    -e OB_TRUSTED_PROXIES=192.0.2.2/32 -e OB_RUSTFS_HOST=rustfs.example.com \
    -e "OB_OPERATOR_ALLOW=192.0.2.9/32" -e "OB_RUSTFS_CONSOLE_ALLOW=100.100.1.2/32" \
    -e "OB_RUSTFS_CONSOLE=$enabled" -e "OB_RUSTFS_URL_HOST=$url_host" -e "OB_RUSTFS_AUTHORITY=$rustfs_authority" \
    -e "OB_GRAFANA_URL_HOST=$url_host" -e "OB_GRAFANA_AUTHORITY=$authority" \
    -v "$root/docker/caddy/Caddyfile:/etc/caddy/Caddyfile:ro" "$caddy_image" \
    caddy adapt --validate --config /etc/caddy/Caddyfile > "$work/caddy-$1-$2-$enabled.json"
done
done
python3 - "$work" <<'PY'
import json, sys
from pathlib import Path

def objects(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from objects(child)

def status_route(value):
    return any(isinstance(match, dict) and '/status.json' in match.get('path', [])
               for match in value.get('match', []))

for path in Path(sys.argv[1]).glob('caddy-*.json'):
    config = json.loads(path.read_text())
    servers = config['apps']['http']['servers'].values()
    listeners = {address for server in servers for address in server['listen']}
    assert listeners == ({':80'} if path.name.startswith('caddy-proxy-') else {':80', ':443'}), path
    for server in servers:
        assert server['trusted_proxies']['ranges'] == ['192.0.2.2/32']
        assert server['trusted_proxies_strict']
    encoded = json.dumps(config)
    status_routes = [item for item in objects(config) if status_route(item)]
    assert status_routes, f'{path}: missing adapted /status.json route'
    for route in status_routes:
        handlers = {item.get('handler') for item in objects(route)}
        assert {'file_server', 'static_response'} <= handlers, (
            f'{path}: /status.json must terminate in file or empty static responses')
        assert 'Location' not in json.dumps(route), f'{path}: /status.json redirects'
    enabled = path.stem.endswith('-true')
    assert ('rustfs:9001' in encoded) == (enabled or not path.name.startswith('caddy-public-'))
    # Check the ordered route containing each RustFS proxy, not unrelated health gates.
    rustfs_proxies = [item for item in objects(config) if item.get('handler') == 'reverse_proxy'
                     and {'dial': 'rustfs:9001'} in item.get('upstreams', [])]
    assert bool(rustfs_proxies) == ('rustfs:9001' in encoded), f'{path}: missing RustFS proxy assertion target'
    protected = 0
    denial_match = [{'not': [{'client_ip': {'ranges': ['100.100.1.2/32']}}]}]
    for item in objects(config):
        if item.get('handler') != 'subroute':
            continue
        routes = item.get('routes', [])
        for index, route in enumerate(routes):
            for handler in route.get('handle', []):
                if handler not in rustfs_proxies:
                    continue
                assert any(earlier.get('match') == denial_match and
                           any(response.get('handler') == 'static_response' and
                               str(response.get('status_code')) == '404' for response in earlier.get('handle', []))
                           for earlier in routes[:index]), f'{path}: RustFS proxy lacks a preceding operator denial'
                protected += 1
    assert protected == len(rustfs_proxies), f'{path}: RustFS proxy outside the gated route'
    operator_paths = ('/versions.json', '/health/grafana', '/health/loki', '/health/tempo',
                      '/health/mimir', '/health/alloy', '/health/gateway', '/health/backplane',
                      '/health/alerts')
    for operator_path in operator_paths:
        matched_routes = [item for item in objects(config) if any(
            isinstance(match, dict) and operator_path in match.get('path', [])
            for match in item.get('match', []))]
        assert matched_routes, f'{path}: missing {operator_path} operator route'
        for route in matched_routes:
            ranges = [item['client_ip']['ranges'] for item in objects(route) if 'client_ip' in item]
            assert ['192.0.2.9/32'] in ranges, f'{path}: {operator_path} lacks monitoring operator allowlist'
            assert ['100.100.1.2/32'] not in ranges, f'{path}: {operator_path} uses RustFS console allowlist'
    if path.name.startswith('caddy-proxy-darkforge.tail694fe2.ts.net-'):
        assert 'darkforge.tail694fe2.ts.net:8447' in encoded
        assert 'darkforge.tail694fe2.ts.net:8451' in encoded
        assert 'http.request.hostport' in encoded
        assert 'grafana:3000' in encoded and '/health/grafana' in encoded
    if path.name.startswith('caddy-public-'):
        assert 'https://observe.example.com' in encoded and '308' in encoded
        assert '/health/grafana' in encoded
        assert '"module": "acme"' in encoded
        assert any('/status.json' in json.dumps(server) and 'Location' in json.dumps(server)
                   for server in servers), f'{path}: public HTTP route lacks status bypass'
    if path.name.startswith('caddy-local-'):
        assert '"module": "internal"' in encoded
        assert '/rustfs/console/' in encoded
        # Every local redirect must be the relative native-console landing.
        locations = [value for item in objects(config) if item.get('handler') == 'static_response'
                     for key, value in item.get('headers', {}).items() if key.lower() == 'location']
        assert all(value == ['/rustfs/console/'] for value in locations), f'{path}: unexpected local redirect'
    for logger in config['logging']['logs'].values():
        assert logger['writer']['output'] in ('stdout', 'stderr')
        assert logger['encoder']['wrap']['format'] == 'json'
        assert logger['encoder']['fields']['request>headers']['filter'] == 'delete'
PY
echo 'Caddyfile (3 access modes, trusted proxy, RustFS off/on): PASS'
for enabled in true false; do
for token in '' validation-only; do
  docker run --rm --log-driver=journald --log-opt cache-disabled=true \
    -e "OB_SCRAPE_EDGE=$enabled" -e "OB_SCRAPE_GATEWAY=$enabled" -e "OB_SCRAPE_BACKPLANE=$enabled" \
    -e "OB_BACKPLANE_OPERATIONS_TOKEN=$token" \
    -v "$root/config.alloy:/etc/alloy/config.alloy:ro" "$alloy_image" \
    validate /etc/alloy/config.alloy
done
done
echo 'Alloy config (Edge/gateway/backplane enabled/disabled, token absent/present): PASS'
echo 'Grafana provisioning: checked by smoke.sh against the running Grafana APIs'
