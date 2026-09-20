# Public status observations

`GET /status.json` and `HEAD /status.json` on the Stack Console origin expose the
version 1 public allowlist in local HTTP/HTTPS, public HTTP/HTTPS and proxy modes.
Public HTTP serves this exact path without redirecting. Other methods receive an
empty 405; a missing document receives an empty 404. Responses use JSON and
`Cache-Control: no-store`. Caddy removes authorization, proxy authorization, cookies,
conditional cache headers and range requests before serving the file. No request is
forwarded to a backend. The observer's private records are outside Caddy's mount.

The host observer uses Python's standard library and the existing Docker CLI. Run it
as the installation owner, an ordinary trusted host user with Docker access, file
ownership and read access to the chosen env file. Docker access remains host-root
equivalent authority. There is no new container, privileged service, published port,
package dependency or Caddy Docker socket mount.

## Select and enable

Run one observation with absolute installation paths:

```sh
python3 /path/to/observability-stack/scripts/status_observer.py \
  --checkout /path/to/observability-stack \
  --env-file /path/to/observability-stack/.env
```

Selection is explicit. Native Compose resolves the selected env file, including
`COMPOSE_FILE`, profiles, project name and full `OB_*_IMAGE` overrides. Interactive
shell and user-manager `OB_*`/`COMPOSE_*` overrides are discarded. Persist desired
settings in the selected env file. Docker connection settings are retained. Bootstrap
saves its project, state directory and volume prefix selections in that env file.
Relative Compose paths resolve from the selected checkout. The output directory comes from
Caddy's effective read-only `/srv/state` bind mount; it must end in `console`.
Normally this is `OB_STATE_DIR/console`, default `data/console`. The observer never
falls back to another installation when configuration resolution fails.

Bootstrap records its preparation task after env/access/storage validation. It saves
an unknown in-progress record, then success or failure, including the actual invocation
start time. An interrupted process leaves unknown. Validation refusals and render-only
runs create no task record. After readiness succeeds bootstrap attempts an initial
observation; observation failure prints a fixed warning and preserves bootstrap success.
Recording failures only warn and preserve the original bootstrap result.
Bootstrap never installs or enables a timer.

For periodic observations, explicitly opt in:

```sh
python3 /path/to/observability-stack/scripts/install_status_timer.py \
  --checkout /path/to/observability-stack \
  --env-file /path/to/observability-stack/.env --install
systemctl --user status observability-status.timer
```

The installer writes `observability-status.service` and `.timer` under
`${XDG_CONFIG_HOME:-~/.config}/systemd/user`, reloads the user manager, and enables the
timer. The service is `Type=oneshot`, with a 90-second process deadline. The timer
starts after 10 seconds and uses a 30-second activation interval with one-second
accuracy. A run exceeding the interval delays the next activation; systemd never
starts two copies of the same service. A private lock excludes manual concurrent runs.
Existing units are refused, including a different checkout selection. Inspect and
explicitly disable/remove the old pair before reinstalling. Unit-installation failure
may leave written units for private inspection; retry does not overwrite them.

The user manager must remain active and retain Docker access. Lingering is an operator
choice; this installer does not change it. Stop observation with
`systemctl --user disable --now observability-status.timer`. The last document stays
on disk and expires. Inspect generic observer failures with
`journalctl --user -u observability-status.service`. Diagnose Compose and Docker
privately; their complete output can contain secrets.

## Evidence and effects

All HTTP probes dial the container IPv4 address on the selected Compose default
network from the host, without redirects, cookies, authorization or proxy environment.
The observer checks a local Unix-socket Docker context, rootful security options and
a local bridge network before dialing. This requires Linux host access to bridge
addresses. Remote contexts, rootless Docker, missing IPv4 addresses and unsupported
network layouts leave readiness unknown. A local bridge blocked by a firewall produces
unavailable when its probe fails. A Unix socket proxy to a remote daemon is outside the
trusted local-daemon assumption. Do not use one for this observer.

| Component | Probe and healthy evidence | Runtime version | Limits |
| --- | --- | --- | --- |
| Caddy | HTTP `:80/health/status`, status 200 and body `ok`, selected root Host header | `caddy version` inside the inspected container | Confirms the HTTP route only; no TLS, certificates or upstream readiness |
| Grafana | HTTP `:3000/api/health`, status 200 and JSON `database: ok` | `version` in the same response | Does not query datasources, alerts or dashboards |
| Alloy | HTTP `:12345/-/ready`, status 200 | `alloy --version` inside the inspected container | Initial configuration loaded; no successful scrape, delivery or component-health claim |
| Loki | HTTP `:3100/ready`, status 200 | JSON `version` from `/loki/api/v1/status/buildinfo` | Readiness only; no log write/query |
| Mimir | HTTP `:8080/ready`, status 200 | JSON `data.version` from `/api/v1/status/buildinfo`, with `status: success` | Readiness only; no metric write/query |
| Tempo | HTTP `:3200/ready`, status 200 | Parsed plain-text release from `/status/version` | Readiness only; no trace write/query |
| RustFS | HTTP `:9000/health/ready`, status 200 | `rustfs --version` inside the inspected container | Storage/IAM/peer readiness; no bucket or object mutation |
| rustfs-init | Docker task state, real `StartedAt`, exit code and valid `FinishedAt` | Omitted | Last execution success, failure or observed running task; does not recheck buckets |
| bootstrap | Private execution record bound to checkout and env path | Omitted | Last preparation execution; no current readiness claim |

Readiness paths returning 401, 403 or 404 are unsupported/unknown; other non-200
responses and bounded exchange failures are unavailable. Unsupported or malformed
version responses remain unknown independently of readiness. Docker's health label is
never used. An inspected stopped/dead/paused service is unavailable; restarting is
starting. Running alone stays unknown. A nonempty project inventory with no configured
service resource establishes absent. An empty or failed inventory, multiple replicas, unexpected
labels or malformed inspection stays unknown. A task with no record stays unknown.
Runtime probes are bracketed by inspections of the same container and start time;
identity changes discard runtime evidence.

These reads may update application access logs and internal counters. Three version
probes execute short-lived binaries inside existing containers using
`timeout -s KILL 3`; a missing executable or timeout utility yields unknown version.
No shell, package installation, reload, restart, user-data query, telemetry ingestion
or object write is performed. A custom image may not implement the shipped probe.

RustFS and rustfs-init are disabled only when both are omitted from effective services,
the installation storage marker says `filesystem`, and all three backend commands and
mounted configuration contents match the reviewed filesystem configurations. Profile
omission alone, a missing marker, extra flags, changed files or an S3 mount stays
unknown. Enabled RustFS remains configured even if its container is missing.

Telemetry is `configured` only when the effective Alloy command and mounted file bytes
match the reviewed collection configuration in `status_config.py`. This proves selected
configuration, without asserting that the running Alloy has reloaded it or delivered
signals. Missing, changed, unreadable or unsupported configurations yield unknown.
Review and update the allowlisted hashes when deliberately changing these configs.

## Freshness, size and custody

The document contains nine fixed components, at most 32, and is capped at 64 KiB.
Configuration and component observations have separate 120-second validity windows.
Each component is dated at the start of its own bounded inspection/probe transaction;
configuration is dated before its inspection. `generatedAt` only dates assembly.
Task `lastExecutionAt` is its recorded start, separate from record inspection time.
Consumers must enforce timestamps, TTL and clock agreement on every render. Serving
an unchanged file never renews its facts. The producer does not reuse successful
runtime probes after failures. Failed configuration or atomic publication leaves the
previous file with its original times; failed individual probes publish their new
outcomes without blocking siblings. Even a task's old execution success requires a
fresh record inspection. Synchronize host and consumer clocks.

At most four components run concurrently. Compose configuration gets 10 seconds and
1 MiB combined subprocess output; inspect gets four seconds and 1 MiB. Other commands
get four seconds and 64 KiB unless carrying encoded HTTP output (512 KiB). HTTP bodies
are capped at 64 KiB and a child process bounds each entire exchange to four seconds,
including trickled headers. Python kills only its directly spawned child on timeout;
the in-container version command has its own three-second deadline. Private files
are bounded reads. No backend diagnostic bodies or command errors enter public JSON.

`configuredVersion` is an allowlisted release tag or `custom`. `observedVersion` comes
only from a runtime endpoint or binary. `configuredDigest` is a registry manifest
digest; `observedImageId` is Docker's local image content ID. They identify different
objects and must not be compared as a convergence check. Only lowercase complete
SHA-256 identifiers and tightly parsed release strings are published. Container names,
IDs, addresses, host paths, environments, commands, credentials and errors are excluded.

Publication writes a same-directory temporary file, flushes and fsyncs it, then replaces
the public file atomically. Public JSON is mode 0644. Private task records are mode
0600 under `OB_STATE_DIR/status` mode 0700. The output directory must be owned by the
observer user without group/other write permission; symlinked directory paths and
symlink/hardlink output files are refused. Bootstrap creates the console directory with mode 0755 subject to the operator umask;
existing directory permissions remain unchanged. For an existing installation with group-writable console metadata, the owner
must correct its permissions before running the observer. Do not put secrets in the
console directory.

## Runtime acceptance

Run `scripts/validate.sh`, `python3 -m unittest discover -s tests`, and
`scripts/smoke.sh` from the accepted checkout. Validate and smoke require the root
runner's Docker authorization. Also verify local HTTP/HTTPS, public HTTP/HTTPS and
proxy GET/HEAD, empty 405 for POST, missing-file 404, JSON/no-store headers, conditional
requests, and identical credential-free results with arbitrary cookies/authorization.
Check effective filesystem and S3 configurations, native custom image overrides,
component failures, stale frozen files after 120 seconds, task execution dates and
observed image IDs. Check each shipped runtime's version output, especially optional
binary timeout support and Tempo's version response format. These are runtime gates;
fake-boundary tests do not establish them.

Primary references: [Grafana health](https://grafana.com/docs/grafana/latest/developer-resources/api-reference/http-api/api-legacy/other/),
[Alloy readiness](https://grafana.com/docs/alloy/latest/reference/http/),
[Loki API](https://grafana.com/docs/loki/latest/reference/loki-http-api/),
[Mimir API](https://grafana.com/docs/mimir/latest/references/http-api/),
[Tempo API](https://grafana.com/docs/tempo/latest/api_docs/),
[RustFS readiness](https://docs.rustfs.com/en/operations/status-check),
[Caddy commands](https://caddyserver.com/docs/command-line), and
[Docker bridge networking](https://docs.docker.com/engine/network/drivers/bridge/).

If unit activation fails after files were written, inspect `systemctl --user status
observability-status.timer` before retrying. Disable the selected timer and remove
only the two generated units before reinstalling; partial activation may already
have started observation. The installer deliberately retains those units for
inspection and will not overwrite them. A concurrent observer holding this
installation's lock makes a new invocation a successful no-op.
