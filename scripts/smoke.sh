#!/bin/sh
# Smoke Contract. Owns only a fresh disposable project's volumes and network.
set -eu
root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
cd "$root"
export COMPOSE_PROJECT_NAME="${SMOKE_PROJECT:-observability-smoke}"
case "$COMPOSE_PROJECT_NAME" in
  *smoke*) ;;
  *) echo 'SMOKE_PROJECT must contain smoke' >&2; exit 2 ;;
esac
[ "$COMPOSE_PROJECT_NAME" != observability-stack ] || exit 2
http_port=${SMOKE_HTTP_PORT:-18180}
https_port=${SMOKE_HTTPS_PORT:-18543}
network="$COMPOSE_PROJECT_NAME-platform"
# Disjoint from the installed Platform Network (172.30.0.0/24); Docker refuses overlapping subnets.
subnet=${SMOKE_PLATFORM_SUBNET:-172.31.$(( $(printf %s "$COMPOSE_PROJECT_NAME" | cksum | cut -d' ' -f1) % 256 )).0/24}
gateway=$(python3 -c 'import ipaddress, sys; print(next(ipaddress.IPv4Network(sys.argv[1]).hosts()))' "$subnet")
# Fail before installing a cleanup trap if this name is already in use.
docker info >/dev/null
[ -z "$(docker ps -aq --filter "label=com.docker.compose.project=$COMPOSE_PROJECT_NAME")" ] || { echo 'smoke project already exists' >&2; exit 2; }
[ -z "$(docker volume ls -q --filter "label=com.docker.compose.project=$COMPOSE_PROJECT_NAME")" ] || { echo 'smoke volumes already exist' >&2; exit 2; }
if docker volume ls --format '{{.Name}}' | python3 -c 'import sys; prefix=sys.argv[1]+"_"; sys.exit(not any(line.startswith(prefix) for line in sys.stdin))' "$COMPOSE_PROJECT_NAME"; then
  echo 'smoke volumes already exist' >&2; exit 2
fi
if docker network inspect "$network" >/dev/null 2>&1; then
  echo 'smoke network already exists' >&2; exit 2
fi
work=$(mktemp -d)
env_file="$work/.env"
producer=
# Resolve before installing the trap so cleanup itself needs no Python import.
volumes=$(python3 -c 'import sys; sys.path.insert(0, "scripts"); import bootstrap; print(" ".join(bootstrap.VOLUMES))')
cleanup() {
  result=$?
  trap - EXIT HUP INT TERM
  if [ -n "$producer" ]; then
    docker rm -f "$producer" >/dev/null || result=1
  fi
  if [ "$result" -ne 0 ]; then
    docker compose --env-file "$env_file" ps -a >&2 || true
  fi
  docker compose --env-file "$env_file" down -v --remove-orphans >/dev/null || result=1
  for volume in $volumes; do
    name="${COMPOSE_PROJECT_NAME}_$volume"
    if docker volume inspect "$name" >/dev/null 2>&1; then
      docker volume rm "$name" >/dev/null || result=1
    fi
  done
  docker network rm "$network" >/dev/null || result=1
  rm -rf "$work"
  exit "$result"
}
# Isolate settings from the installed stack and from the caller's Compose overrides.
profile=${SMOKE_PROFILE:-filesystem}
case "$profile" in
  filesystem) profiles=; rustfs_console=false ;;
  s3) profiles=s3; rustfs_console=true ;;
  *) echo 'SMOKE_PROFILE must be filesystem or s3' >&2; exit 2 ;;
esac
unset COMPOSE_FILE COMPOSE_PROFILES COMPOSE_ENV_FILES
for key in $(env | sed -n 's/^\(OB_[A-Z0-9_]*\)=.*/\1/p'); do
  unset "$key"
done
# Root runs the proxy variant separately to exercise one hostname on several ports.
access_mode=${SMOKE_ACCESS_MODE:-local}
grafana_url=
rustfs_url=
gateway_url=
backplane_url=
case "$access_mode" in
  local) ;;
  proxy)
    grafana_url=https://darkforge.tail694fe2.ts.net:8447
    rustfs_url=https://darkforge.tail694fe2.ts.net:8451
    gateway_url=https://darkforge.tail694fe2.ts.net:8443
    backplane_url=https://darkforge.tail694fe2.ts.net:8445
    ;;
  *) echo 'SMOKE_ACCESS_MODE must be local or proxy' >&2; exit 2 ;;
esac
sed -e "s#^OB_ACCESS_MODE=.*#OB_ACCESS_MODE=$access_mode#" \
    -e "s#^OB_GRAFANA_URL=.*#OB_GRAFANA_URL=$grafana_url#" \
    -e "s#^OB_RUSTFS_URL=.*#OB_RUSTFS_URL=$rustfs_url#" \
    -e "s#^OB_RUSTFS_CONSOLE=.*#OB_RUSTFS_CONSOLE=$rustfs_console#" \
    -e "s#^OB_GATEWAY_URL=.*#OB_GATEWAY_URL=$gateway_url#" \
    -e "s#^OB_BACKPLANE_URL=.*#OB_BACKPLANE_URL=$backplane_url#" \
    -e "s#^OB_HTTP_PORT=.*#OB_HTTP_PORT=$http_port#" \
    -e "s#^OB_HTTPS_PORT=.*#OB_HTTPS_PORT=$https_port#" \
    -e "s#^OB_PUBLIC_PORT_SUFFIX=.*#OB_PUBLIC_PORT_SUFFIX=:$http_port#" \
    -e "s#^OB_PLATFORM_NETWORK=.*#OB_PLATFORM_NETWORK=$network#" \
    -e "s#^OB_PLATFORM_SUBNET=.*#OB_PLATFORM_SUBNET=$subnet#" \
    -e "s#^OB_PLATFORM_IP_RANGE=.*#OB_PLATFORM_IP_RANGE=$subnet#" \
    -e "s#^OB_VOLUME_PREFIX=.*#OB_VOLUME_PREFIX=$COMPOSE_PROJECT_NAME#" \
    -e "s#^OB_ALERTS=.*#OB_ALERTS=placeholder#" \
    -e "s#^OB_RUSTFS_CONSOLE_ALLOW=.*#OB_RUSTFS_CONSOLE_ALLOW=127.0.0.1/8 ::1#" \
    -e "s#^OB_OPERATOR_ALLOW=.*#OB_OPERATOR_ALLOW=private_ranges#" \
    -e "s#^OB_STATE_DIR=.*#OB_STATE_DIR=$work/data#" \
    -e "s#^OB_BACKUP_DIR=.*#OB_BACKUP_DIR=$work/backups#" \
    -e "s#^COMPOSE_PROFILES=.*#COMPOSE_PROFILES=$profiles#" \
    -e "s#^OB_SCRAPE_GATEWAY=.*#OB_SCRAPE_GATEWAY=false#" \
    -e "s#^OB_SCRAPE_BACKPLANE=.*#OB_SCRAPE_BACKPLANE=false#" \
    .env.example > "$env_file"
chmod 600 "$env_file"
docker network create --driver bridge --subnet "$subnet" --ip-range "$subnet" --gateway "$gateway" "$network" >/dev/null
trap cleanup EXIT HUP INT TERM
# Permit only this disposable project's host ingress peer.
sed -i "s#^OB_RUSTFS_CONSOLE_ALLOW=.*#OB_RUSTFS_CONSOLE_ALLOW=$gateway#" "$env_file"
image=$(python3 -c 'import sys; from pathlib import Path; sys.path.insert(0, "scripts"); import bootstrap; print(bootstrap.images(Path("compose.yaml"))["caddy"])')
producer=$(docker run -d --network none --log-driver=journald --log-opt cache-disabled=true \
  --entrypoint sh "$image" -c 'echo independent-stdout; echo independent-stderr >&2')
docker wait "$producer" >/dev/null
docker logs "$producer" > "$work/producer.stdout" 2> "$work/producer.stderr"
grep -q independent-stdout "$work/producer.stdout"
grep -q independent-stderr "$work/producer.stderr"
docker rm "$producer" >/dev/null
producer=
echo 'ok: journald Docker API reads stdout/stderr before Collector startup, cache disabled'
python3 scripts/bootstrap.py --env-file "$env_file"
echo 'ok: bootstrap readiness'
docker compose --env-file "$env_file" ps -a --format json > "$work/services.jsonl"
python3 - "$work/services.jsonl" "$profile" <<'PY'
import json,sys
expected = {'caddy':'healthy','grafana':'healthy','alloy':'healthy','loki':'','tempo':'','mimir':''}
if sys.argv[2] == 's3':
    expected['rustfs'] = 'healthy'
seen = {}
for line in open(sys.argv[1]):
    c = json.loads(line)
    if c['Service'] == 'rustfs-init':
        assert c['State'] == 'exited' and c['ExitCode'] == 0, c
        continue
    assert c['State'] == 'running', c['Service'] + ' not running'
    seen[c['Service']] = c.get('Health','')
    assert c['Service'] == 'caddy' or not any(p.get('PublishedPort') for p in c.get('Publishers') or []), c['Service']
assert seen == expected, seen
PY
echo 'ok: exact service set, Docker health, only Caddy published'
python3 scripts/smoke_assertions.py "$env_file" "localhost:$http_port" "$COMPOSE_PROJECT_NAME"

if [ "$profile" = s3 ]; then
  python3 scripts/smoke_s3.py "$env_file" "localhost:$http_port"
fi
