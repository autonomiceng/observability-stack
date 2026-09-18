#!/bin/sh
# Static gates and image-provided config validators. No stack is started.
set -eu
root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
cd "$root"
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
echo 'env render: PASS'
for mode in filesystem s3; do
  if [ "$mode" = s3 ]; then
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
    unpinned = []
    for name, svc in services.items():
        ref = svc.get('image', '')
        if not re.fullmatch(r'.+:[^:@]+@sha256:[0-9a-f]{64}', ref) or ':latest@' in ref:
            unpinned.append(f'{name}: {ref}')
        if svc.get('restart') == 'unless-stopped':
            assert 0 < int(svc['mem_reservation']) < int(svc['mem_limit']), name
        assert not svc.get('env_file'), f'{name}: list environment explicitly'
    if unpinned:
        sys.exit('unresolved image digests (resolve with docker buildx imagetools inspect):\n' + '\n'.join(unpinned))
    expected = {'caddy','grafana','alloy','loki','tempo','mimir'}
    if path.stem == 's3':
        expected |= {'rustfs','rustfs-init'}
        for name in ('loki','tempo','mimir'):
            assert any(v.get('source', '').endswith('/s3.yaml') for v in services[name]['volumes']), name
    assert set(services) == expected, f'{path}: unexpected services'
    assert services['tempo'].get('stop_grace_period') == '45s', 'Tempo stop grace'
PY
echo 'compose config and pins: PASS'
caddy_image=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["services"]["caddy"]["image"])' "$work/filesystem.json")
alloy_image=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["services"]["alloy"]["image"])' "$work/filesystem.json")
for mode in 'http localhost none' 'https example.com acme' 'https example.com internal'; do
  # Each mode is a fixed three-word tuple.
  # shellcheck disable=SC2086
  set -- $mode
  docker run --rm -e "OB_LISTEN_SCHEME=$1" -e "OB_PUBLIC_DOMAIN=$2" -e "OB_TLS_ISSUER=$3" \
    -v "$root/docker/caddy/Caddyfile:/etc/caddy/Caddyfile:ro" "$caddy_image" \
    caddy validate --config /etc/caddy/Caddyfile >/dev/null
done
docker run --rm -e OB_LISTEN_SCHEME=http -e OB_PUBLIC_DOMAIN=observe.example.com -e OB_TLS_ISSUER=none \
  -e 'OB_TRUSTED_PROXIES=172.30.0.2/32' -e 'OB_OPERATOR_ALLOW=192.0.2.10/32' \
  -v "$root/docker/caddy/Caddyfile:/etc/caddy/Caddyfile:ro" "$caddy_image" \
  caddy validate --config /etc/caddy/Caddyfile >/dev/null
echo 'Caddyfile (3 access modes and trusted proxy): PASS'
for enabled in true false; do
for token in '' validation-only; do
  docker run --rm -e "OB_SCRAPE_GATEWAY=$enabled" -e "OB_BACKPLANE_OPERATIONS_TOKEN=$token" \
    -v "$root/config.alloy:/etc/alloy/config.alloy:ro" "$alloy_image" \
    validate /etc/alloy/config.alloy
done
done
echo 'Alloy config (gateway enabled/disabled, token absent/present): PASS'
echo 'Grafana provisioning: checked by smoke.sh against the running Grafana APIs'
