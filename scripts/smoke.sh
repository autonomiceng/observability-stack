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
case "$access_mode" in
  local) ;;
  proxy)
    grafana_url=https://host.tail-example.ts.net:8447
    rustfs_url=https://host.tail-example.ts.net:8451
    ;;
  *) echo 'SMOKE_ACCESS_MODE must be local or proxy' >&2; exit 2 ;;
esac
sed -e "s#^OB_ACCESS_MODE=.*#OB_ACCESS_MODE=$access_mode#" \
    -e "s#^OB_GRAFANA_URL=.*#OB_GRAFANA_URL=$grafana_url#" \
    -e "s#^OB_RUSTFS_URL=.*#OB_RUSTFS_URL=$rustfs_url#" \
    -e "s#^OB_RUSTFS_CONSOLE=.*#OB_RUSTFS_CONSOLE=$rustfs_console#" \
    -e "s#^OB_HTTP_PORT=.*#OB_HTTP_PORT=$http_port#" \
    -e "s#^OB_HTTPS_PORT=.*#OB_HTTPS_PORT=$https_port#" \
    -e "s#^OB_PUBLIC_PORT_SUFFIX=.*#OB_PUBLIC_PORT_SUFFIX=:$http_port#" \
    -e "s#^OB_PLATFORM_NETWORK=.*#OB_PLATFORM_NETWORK=$network#" \
    -e "s#^OB_PLATFORM_SUBNET=.*#OB_PLATFORM_SUBNET=$subnet#" \
    -e "s#^OB_PLATFORM_IP_RANGE=.*#OB_PLATFORM_IP_RANGE=$subnet#" \
    -e "s#^OB_VOLUME_PREFIX=.*#OB_VOLUME_PREFIX=$COMPOSE_PROJECT_NAME#" \
    -e "s#^OB_RUSTFS_CONSOLE_ALLOW=.*#OB_RUSTFS_CONSOLE_ALLOW=127.0.0.1/8 ::1#" \
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

if [ "$access_mode" = local ]; then
  # Operator certificate files: a throwaway CA signs one leaf for every Local Mode HTTPS name.
  # OpenSSL rejects a wildcard directly under a single-label domain such as *.localhost.
  mkdir "$work/certs"
  printf 'basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=serverAuth\nsubjectAltName=DNS:localhost,DNS:grafana.localhost,DNS:rustfs.localhost\n' > "$work/leaf.cnf"
  openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -noenc -keyout "$work/ca.key" -out "$work/ca.crt" \
    -subj '/CN=observability-stack smoke CA' -days 2 -addext 'basicConstraints=critical,CA:TRUE' \
    -addext 'keyUsage=critical,keyCertSign,cRLSign' 2>/dev/null
  openssl req -newkey ec -pkeyopt ec_paramgen_curve:P-256 -noenc -keyout "$work/certs/tls.key" -out "$work/leaf.csr" \
    -subj '/CN=localhost' 2>/dev/null
  openssl x509 -req -in "$work/leaf.csr" -CA "$work/ca.crt" -CAkey "$work/ca.key" -CAcreateserial -out "$work/certs/tls.crt" \
    -days 2 -extfile "$work/leaf.cnf" 2>/dev/null
  # Caddy reads the key as uid 0 without CAP_DAC_OVERRIDE; this throwaway key stays inside the private work directory.
  chmod 0644 "$work/certs/tls.key"
  docker compose --env-file "$env_file" --env-file "$work/data/derived.env" exec -T caddy \
    cat /data/caddy/pki/authorities/local/root.crt > "$work/root.crt"
  # Bootstrap records the shell choices and the overlay in the env file.
  OB_TLS_ISSUER=files OB_TLS_DIR="$work/certs" OB_TLS_CA="$work/ca.crt" \
    python3 scripts/bootstrap.py --env-file "$env_file" >/dev/null
  expected=compose.yaml:${profiles:+compose.s3.yaml:}compose.files.yaml
  [ "$(sed -n 's/^COMPOSE_FILE=//p' "$env_file")" = "$expected" ] || { echo 'files issuer: COMPOSE_FILE lacks the overlay' >&2; exit 1; }
  echo 'ok: files issuer: bootstrap recorded the overlay and verified HTTPS readiness against OB_TLS_CA'
  for target in localhost/health/grafana grafana.localhost/login; do
    host=${target%%/*}
    url="https://$host:$https_port/${target#*/}"
    code=$(curl --noproxy '*' --max-time 10 --cacert "$work/ca.crt" -sS -o /dev/null -w '%{http_code}' \
      --resolve "$host:$https_port:127.0.0.1" "$url")
    [ "$code" = 200 ] || { echo "files issuer: $url answered $code" >&2; exit 1; }
    if curl --noproxy '*' --max-time 10 --cacert "$work/root.crt" -sS -o /dev/null --resolve "$host:$https_port:127.0.0.1" \
        "$url" 2>/dev/null; then echo "files issuer: $host still serves the internal CA certificate" >&2; exit 1; fi
  done
  curl --noproxy '*' --max-time 10 --cacert "$work/root.crt" -fsS -o /dev/null "https://127.0.0.1:$https_port/health/status"
  echo 'ok: files issuer: application hostnames verified by the operator CA only; 127.0.0.1 keeps the internal CA'
fi
