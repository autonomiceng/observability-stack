# Runtime logging and collection

The deployment default is Linux Docker with a working host journal. Every Compose service
uses `journald` with `cache-disabled: "true"`. Continuous runtime logs go to stdout/stderr
and the host journal. No Docker JSON/local cache files or application runtime log files are
configured. Journal persistence, size limits, rate limits and retention are host policy;
this repository does not change them. A functioning host journal is required; Alloy and
remote log availability are not required for a producer to start or log.

Docker documents native reads for the [journald driver](https://docs.docker.com/engine/logging/drivers/journald/).
Disabling the [dual logging cache](https://docs.docker.com/engine/logging/dual-logging/) does
not disable those native reads. Alloy's [Docker source](https://grafana.com/docs/alloy/latest/reference/components/loki/loki.source.docker/)
uses the Docker API, so no host journal mount or secondary journal reader is needed. The smoke test verifies this collection path; also check ingestion on your deployment host.

## Runtime logging audit

| Service | Runtime destination | Intentional persistent product state |
| --- | --- | --- |
| Caddy | JSON access logs stdout; JSON runtime diagnostics stderr | Certificates/private keys in `caddy-data`; Caddy config state in `caddy-config` |
| Grafana | `GF_LOG_MODE=console` | SQLite, plugins and provisioning |
| Alloy | Standard process output | Docker positions and metrics remote-write WAL |
| Loki | Standard process output, no file logger configured | Ingested log chunks/index, WAL and compactor state |
| Mimir | Standard process output, no file logger configured | Metrics blocks, WAL, rules and compactor state |
| Tempo | Standard process output, no file logger configured | Trace blocks and WAL |
| RustFS (optional) | Empty `RUSTFS_OBS_LOG_DIRECTORY`, stdout enabled | S3 objects and object-store metadata |
| RustFS init | curl process output | Bucket creation only |

The [RustFS image](https://github.com/rustfs/rustfs/blob/1.0.0/Dockerfile) sets a log directory
by default. Compose explicitly clears it; RustFS documents stdout-only behavior for an empty
directory in its [logging configuration](https://github.com/rustfs/rustfs/blob/main/crates/obs/src/config.rs).
Run the S3 smoke profile to verify this behavior with the pinned image on your host.
Protected one-off Checkpoint diagnostics, archives, database WAL/audit data and Loki's stored
log product are separate from continuous runtime log files. Do not delete these as log cleanup.

Caddy removes request and response headers and the entire query string from both access and
request-bearing diagnostic records. This covers credentials in cookies, Authorization,
custom API-key headers and query parameters. URL paths, host, method, status and duration
remain JSON fields. Never put credentials in URL paths. Request bodies are not access logged.
The built-in [Caddy credential redaction](https://caddyserver.com/docs/caddyfile/directives/log)
remains enabled as well. No file writer or remote log sink is used.

## Platform Edge and optional collection

Host Docker discovery already collects Platform Edge's stdout/stderr, including Caddy JSON
access logs. Enable access logging in Edge itself and use a readable driver there. No Edge
container, metrics endpoint, Alloy address or remote log service is a required dependency
of this deployment or of the Edge producer. `OB_SCRAPE_GATEWAY=false` and `OB_SCRAPE_BACKPLANE=false`
keep sibling metrics targets absent by default.

Loki labels remain `job`, `compose_project`, `service` and `container`, plus Docker stream
metadata. Query Edge with `{job="docker",compose_project="platform-edge"} | json`, adjusting
the project name to the actual deployment. URLs stay in JSON, never Loki labels.
Projects matching `-(smoke|drill)(-|$)` remain excluded.

To collect Edge metrics, set `OB_SCRAPE_EDGE=true`. Alloy then requests
`http://pe-edge:80/metrics` over the shared Docker network. Edge serves this internal
address in all access modes. Reserve a stable IP for Alloy on that network and add only
that IP (`/32` for IPv4 or `/128` for IPv6) to Edge's `PE_METRICS_ALLOW`. Recreate the
containers through the normal deployment procedure after changing these settings. Never
allow a whole network or the Docker bridge address to make a scrape succeed. Leave the
switch off when Edge is not installed; observability starts independently either way.

## Evidence and portability

Run `scripts/smoke.sh` with a unique `SMOKE_PROJECT`, `SMOKE_HTTP_PORT` and `SMOKE_HTTPS_PORT`.
It starts a disconnected journald producer before bootstrap, reads stdout/stderr through
`docker logs` with the cache disabled, then proves an Edge-labelled disposable producer
reaches Loki through Alloy. That fixture is evidence for the pipeline, not evidence that
an installed Platform Edge is emitting or being ingested. Confirm the installed Edge with
an actual request followed by the LogQL query above. Smoke also checks Caddy stream routing,
header/query redaction, local HTTP/HTTPS and the absence of redirects and HSTS.

On Docker Desktop or a daemon without journald, explicitly change the `x-logging` mapping
in the deployment's `compose.yaml` to `driver: none` and `options: {}` before bootstrap.
This disables runtime retention and Docker API collection for every service. A bounded
`local` driver is another portability choice, but it writes Docker log files and therefore
changes this deployment's logging policy. There is no automatic fallback. Keep that operator
change under review when updating the checkout. The journald validation and Smoke Contract
intentionally require the default policy and do not certify a portability change.
