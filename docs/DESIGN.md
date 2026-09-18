# observability-stack: systems design

One host, one Compose project, one place to investigate the gateway and backplane.
Vocabulary lives in `../CONTEXT.md`; the Owner's binding decision is ADR-0001.

## Shape

```text
Browser -> Caddy (:80/:443 on OB_BIND_HOST) -> Grafana
              |                              | queries
              + readiness probes             + Loki / Mimir / Tempo

Docker socket -> Alloy -> Loki (logs, 30d)
lg-litellm:4000/metrics --+
bp-server:3000/metrics --+-> Alloy -> Mimir (metrics, 30d)
Alloy self / cAdvisor / textfile --+
OTLP producer -> Alloy :4317/:4318 -> Tempo (traces, 7d)
```

Caddy joins `platform` as `ob-gateway`, Grafana as `ob-grafana`; Alloy joins as `ob-alloy`.
Every service also uses the project's default network. Loki, Tempo, Mimir
and optional RustFS never join `platform`. Only Caddy publishes host ports.

## Collection

Alloy discovers the host's running Docker containers, follows stdout/stderr through the
Docker API, and adds `compose_project`, `service`, `container` and `job=docker` labels.
The `compose_project` and `service` label names are the collection interface.
Compose projects matching `-(smoke|drill)(-|$)` are excluded from log collection.
Containers outside Compose retain their container label. Docker must support its logs API;
containers configured with the `none` log driver have no stream to collect.

Alloy scrapes LiteLLM, gateway Valkey/Postgres exporters and gateway Checkpoint metrics
by default (`OB_SCRAPE_GATEWAY=true`). Set it
to false for a standalone install; a hidden-label relabel rule removes the target. Another
relabel rule removes the backplane target while `OB_BACKPLANE_OPERATIONS_TOKEN` is empty; a nonempty
token enables the authenticated scrape. Enabled but missing stacks produce failed scrapes; disabled targets are absent, while Collector readiness stays independent of scrape success. Mimir receives
remote-write at `/api/v1/push`; Grafana queries its `/prometheus` API. Embedded cAdvisor
supplies container resource metrics. Embedded textfile
collection reads `OB_STATE_DIR/textfile/*.prom`. The same unix exporter collects host
filesystem bytes/inodes through `/rootfs`; Alloy also scrapes all three Backends.
The gateway supplies its Valkey and Postgres exporter containers. OTLP passes through a
memory limiter before batching; each
long-lived service has a memory reservation and hard limit. See `operations/capacity.md`.

Alloy binds OTLP only to its default-network alias `ob-alloy-otlp`. An opt-in producer
on that network uses `ob-alloy-otlp:4317` or `http://ob-alloy-otlp:4318`. There is no
producer provisioned here. Gateway LLM payload traces remain in Langfuse.

## Storage and provisioning

External volumes use `OB_VOLUME_PREFIX` (default `observability-stack`) and survive
`compose down -v`. Bootstrap creates them; `scripts/destroy.sh` requires typed confirmation.
The default volumes hold Grafana's SQLite state, Alloy positions and remote-write WAL,
Loki TSDB/chunks, Mimir blocks/WAL and Tempo local blocks/WAL. Loki's compactor applies 30-day
retention, Mimir's compactor 30 days, and Tempo's scheduler/worker seven days. Deletion is
asynchronous, so retention is a time horizon rather than a strict disk quota.

Set `COMPOSE_PROFILES=s3` before bootstrap for RustFS. Compose profiles can add services but
cannot replace existing mounts, so bootstrap records `COMPOSE_FILE=compose.yaml:compose.s3.yaml`.
The override selects the three S3 configs. A one-shot `rustfs-init` reuses RustFS's image and
SigV4-capable curl to create separate buckets. WALs, compaction state and caches remain local.
The storage-mode marker refuses silent switches. This is a fresh-install option; copying
filesystem data to S3 requires an explicit migration. Checkpoints archive local volumes
and RustFS together in S3 mode. The restore drill has explicit filesystem and S3 modes with historical telemetry and Grafana state checks.

Grafana provisions three fixed datasource UIDs (`loki`, `mimir`, `tempo`), the `Stacks` folder,
one dashboard and thirteen alerts. Anonymous access and user signup are disabled. The admin
password is generated once. Bootstrap requires a webhook or email/SMTP delivery configuration; an explicit
placeholder reports degraded readiness. Generated provisioning lives in `OB_STATE_DIR`.
Metric contracts and absent-source behavior are in `operations/maintenance.md`.

## Health and verification

Caddy serves exact `/health/{grafana,loki,tempo,mimir,alloy}` routes. Optional sibling probes
are `/health/gateway` and `/health/backplane`. Caddy's aggregate Docker healthcheck probes
all five own upstreams. Mimir and Tempo are distroless, so backend readiness is checked over
HTTP from Caddy rather than by installing another executable in their containers. The smoke
contract checks each endpoint as well as container state.

Bootstrap follows the gateway's lock, secret-preservation and existing-data refusal contract.
It creates the platform network, starts Compose with `--wait`, then probes ingress readiness.
Smoke uses separate volumes, network, console state, ports and generated credentials and
refuses an existing disposable project name before installing its cleanup trap.

Grafana does not offer a general offline provisioning validator used here. The smoke contract
checks its authenticated datasource, dashboard and alert APIs after loading the YAML. Alloy's
image validates its config with both gateway toggle values and empty/nonempty backplane token settings. Pull request, weekly and manually dispatched CI
runs both filesystem and S3 smoke; S3 verifies log/metric/trace continuity and object bodies over RustFS restart.
Separate filesystem and S3 recovery jobs restore Checkpoints into empty disposable volumes.
The recovery drill disables host discovery only in its temporary checkout; production smoke
retains host collection assertions.
The fenced Checkpoint scripts preserve the five telemetry volumes, optional RustFS,
configuration and installation marker. The manifest records env key names; operators
retain the original secrets separately. See `operations/backup.md`.

## Scope

No multi-host availability, Pyroscope, OAuth, extra dashboards, producer
changes or automatic backup scheduling. This stack's broad host observation privileges are
intentional; keep Docker and `platform` membership restricted to trusted workloads.
