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
cleanup() {
  result=$?
  trap - EXIT HUP INT TERM
  if [ "$result" -ne 0 ]; then
    docker compose --env-file "$env_file" ps -a >&2 || true
  fi
  docker compose --env-file "$env_file" down -v --remove-orphans >/dev/null || result=1
  for volume in caddy-data caddy-config grafana-data alloy-data loki-data tempo-data mimir-data rustfs-data; do
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
  filesystem) profiles= ;;
  s3) profiles=s3 ;;
  *) echo 'SMOKE_PROFILE must be filesystem or s3' >&2; exit 2 ;;
esac
unset COMPOSE_FILE COMPOSE_PROFILES COMPOSE_ENV_FILES
for key in $(env | sed -n 's/^\(OB_[A-Z0-9_]*\)=.*/\1/p'); do
  unset "$key"
done
sed -e "s#^OB_HTTP_PORT=.*#OB_HTTP_PORT=$http_port#" \
    -e "s#^OB_HTTPS_PORT=.*#OB_HTTPS_PORT=$https_port#" \
    -e "s#^OB_PUBLIC_PORT_SUFFIX=.*#OB_PUBLIC_PORT_SUFFIX=:$http_port#" \
    -e "s#^OB_PLATFORM_NETWORK=.*#OB_PLATFORM_NETWORK=$network#" \
    -e "s#^OB_VOLUME_PREFIX=.*#OB_VOLUME_PREFIX=$COMPOSE_PROJECT_NAME#" \
    -e "s#^OB_ALERTS=.*#OB_ALERTS=placeholder#" \
    -e "s#^OB_OPERATOR_ALLOW=.*#OB_OPERATOR_ALLOW=private_ranges#" \
    -e "s#^OB_STATE_DIR=.*#OB_STATE_DIR=$work/data#" \
    -e "s#^OB_BACKUP_DIR=.*#OB_BACKUP_DIR=$work/backups#" \
    -e "s#^COMPOSE_PROFILES=.*#COMPOSE_PROFILES=$profiles#" \
    -e "s#^OB_SCRAPE_GATEWAY=.*#OB_SCRAPE_GATEWAY=false#" \
    .env.example > "$env_file"
chmod 600 "$env_file"
docker network create "$network" >/dev/null
trap cleanup EXIT HUP INT TERM
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
