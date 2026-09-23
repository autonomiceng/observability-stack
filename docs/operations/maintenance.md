# Maintenance

## Updating images

1. Read upstream release notes, including storage and config migrations. Before editing pins or configuration, run `scripts/backup.sh` with the checkout matching the running installation and retain the original `.env` separately. See [backup and restore](backup.md).
2. Resolve the stable tag with `docker buildx imagetools inspect <ref> --format '{{.Manifest.Digest}}'` and put the complete `tag@sha256` in `compose.yaml`.
3. Run `scripts/validate.sh`, unit tests and `scripts/smoke.sh`, `SMOKE_PROFILE=s3 scripts/smoke.sh`, `SMOKE_PROFILE=filesystem scripts/backup-drill.sh` and `SMOKE_PROFILE=s3 scripts/backup-drill.sh` against the new pins.
4. Verify the pre-change Checkpoint is complete and replicated off-host before applying the upgrade.
5. Pause direct producers. Stop query sources with `docker compose stop -t 120 caddy alloy grafana`,
   run the bounded [Tempo query quiescence check](backup.md#tempo-query-quiescence), then
   run `docker compose stop -t 120 tempo` only after the check succeeds.
   Inspect Tempo's container state and require exit 0 with `OOMKilled=false`; a forced stop
   is a failed gate. See [Tempo query quiescence](backup.md#tempo-query-quiescence).
   Do not query Tempo directly during this fence. Its ordinary 45-second grace alone cannot
   prevent the recent-query shutdown hang.
6. Apply with `docker compose pull` and `python3 scripts/bootstrap.py`, then check readiness,
   Grafana login and historical telemetry before resuming producers. Keep source volumes
   and the pre-change Checkpoint until recovery verification completes.

Restore the entire Checkpoint for a data-format rollback. Reverting only an image tag is safe
only when its release notes promise compatibility with the newer on-disk format. Expect
collection interruption during restarts. Preserve Alloy's volume to retain log positions and
queued remote-write data. Keep `.env` with backups; bootstrap refuses to generate replacement
secrets when installation state exists. Changing the env admin password after Grafana has
initialized does not rotate its stored password; use Grafana's supported password change.

## S3 profile

For a fresh installation, set `COMPOSE_PROFILES=s3`, then run bootstrap. It records the matching
Compose override. Validate with `docker compose config --quiet`; inspect the template with
`docker compose config --no-interpolate`. Interpolated `docker compose config` output includes
Grafana and S3 secrets. Never attach it to issues or public logs. Do not toggle a
running installation between storage modes: bootstrap refuses a mode change, and direct
Compose commands cannot migrate stored data. Checkpoints include RustFS in S3 mode; the automated restore drills exercise both filesystem and S3 modes. RustFS uses the
same pinned image as the gateway but has its own credentials, buckets and volume.

## Alerts and metric contracts

Grafana evaluates the rules in `Stacks / stack-health` every minute. No-data remains visible
as `NoData`, and query failures as `Error`. An absent source cannot prove the threshold safe.
Bootstrap requires `OB_ALERT_WEBHOOK_URL`, or `OB_ALERT_EMAIL` plus `OB_SMTP_URL`.
It renders `OB_STATE_DIR/grafana-provisioning` and the SMTP configuration before starting
Grafana. `OB_STATE_DIR` is made private (mode 0700) because the generated SMTP
configuration may contain credentials. Re-run bootstrap after changing delivery settings. SMTP URLs use
`smtp://user:password@host:587` (required STARTTLS) or `smtps://user:password@host:465`;
percent-encode reserved characters in credentials. The email address is also the sender.
An unauthenticated relay can omit credentials. For a disposable or intentionally undelivered
installation, explicitly set `OB_ALERTS=placeholder`. Bootstrap then reports `degraded`
with `alert_delivery_placeholder`, and `/health/alerts` returns 503 until configured.
Test delivery from Grafana after provisioning.

| Alert | Source and condition |
| --- | --- |
| Valkey memory | `redis_memory_used_bytes / redis_memory_max_bytes >= 0.8`, maxmemory positive, for 5m. Gauges come from job `llm-gateway-valkey` at `lg-valkey-exporter:9121`. |
| Backplane archive age | `bp_archive_lag_seconds{job="backplane"} > 300` for 5m. Existing backplane operations gauge: age of the oldest pending WAL archive. A quiet database does not accumulate archive lag. |
| Checkpoint age | `time() - stack_checkpoint_last_success_timestamp_seconds > 93600` for 5m (26h). Gauge: successful Checkpoint completion timestamp, labelled `stack`. |
| Gateway Checkpoint age | `time() - lg_checkpoint_timestamp_seconds{job="llm-gateway-checkpoints"} > bool 93600` for 5m. |
| Gateway Checkpoint failure | `lg_checkpoint_success{job="llm-gateway-checkpoints"} == bool 0` for 5m. |
| Gateway archiver failure | `increase(pg_stat_archiver_failed_count{job="llm-gateway-postgres"}[15m]) > bool 0` for 5m. |
| Scrape target down | `up{job=~"llm-gateway(-valkey\|-postgres\|-checkpoints)?\|backplane"} == bool 0` for 5m. `bool` returns 1 for a failed target into the > 0 threshold. `OB_SCRAPE_BACKPLANE=false` or `OB_SCRAPE_GATEWAY=false` removes that target. |

Container restart detection is pending a producer for
`container_started_at_seconds{compose_project,service,container}` sourced from Docker
`State.StartedAt`. Alloy's Docker discovery cannot export that value as a Prometheus gauge,
and cAdvisor's `container_start_time_seconds` is container creation time, so it does not
change when the same container restarts.

All four gateway jobs are gated by `OB_SCRAPE_GATEWAY`. Postgres metrics come from
`lg-postgres-exporter:9187`; Checkpoint metrics come from `lg-gateway:8081/metrics`
without a Host override. Existing gateway installations must generate
`LG_POSTGRES_EXPORTER_PASSWORD` through their bootstrap and recreate the exporter before
the Postgres scrape succeeds. No gateway files are changed by this stack.

Observability backup scripts atomically replace a `.prom` file under `OB_STATE_DIR/textfile`
only after success. For example, the resulting file may contain:

```text
# TYPE stack_checkpoint_last_success_timestamp_seconds gauge
stack_checkpoint_last_success_timestamp_seconds{stack="observability-stack"} 1789603200
```

That timestamp is an example, not a heartbeat. Preserve the last success on failure. The
backplane already exposes its archive gauge on its authenticated `/metrics`. Its existing
`bp_backup_age_seconds` is also queryable; the shared Checkpoint rule uses the textfile
contract above. Scraping needs both `OB_SCRAPE_BACKPLANE=true` and
`OB_BACKPLANE_OPERATIONS_TOKEN` set to the backplane's `BP_OPERATIONS_TOKEN`; the token never
becomes a target label, so Alloy's UI and API do not show it. A token without the boolean
leaves the target absent. An enabled scrape with an empty token or a stopped backplane
produces `up{job="backplane"}=0`; Alloy remains ready.

## Investigating missing data

Open Grafana Explore: `up{job="alloy"}` and `up{job="llm-gateway"}` in Mimir,
`{compose_project="llm-gateway-stack"}` in Loki. Request/spend series may be absent until a
request has completed; a successful scrape can still have no request activity. The starter
uses LiteLLM's request counter, total request latency histogram and spend counter. Labels
follow the gateway's exposed `model` series.

Tempo stays empty until a producer opts in. Check `/health/tempo` for readiness; successful
readiness alone does not prove a trace reached storage. The S3 Smoke Contract sends an OTLP
trace, waits for Tempo objects, and queries the original trace after RustFS restarts. It does not exercise retention over multiple days
or force that query to bypass Tempo's local state.

## Resource failure

The six additional rules monitor host free bytes/inodes below 15%, Loki/Mimir/Tempo
ingestion failures, and Alloy remote-write backlog above 10,000 samples, each for five
minutes. Backend `/metrics` endpoints are scraped every 15 seconds. Inspect
`up{job=~"loki|mimir|tempo"}` before trusting those signals. No traffic may leave rejection
series absent; exporter failures and alert `NoData` require investigation.

See [capacity and OOM recovery](capacity.md), [disk-full recovery](disk-full.md) and
[Checkpoint recovery](backup.md). `docker compose down -v` preserves external volumes.
`scripts/destroy.sh` deletes them only after the operator types the project name.

## Status document

Bootstrap writes `OB_STATE_DIR/console/status.json` after every readiness probe passes,
replacing the whole file at once. Caddy serves it as `GET /status.json` to any client, in
every access mode, with `Cache-Control: no-store`; other methods receive an empty 405. It
follows Status v2 in [conventions](../conventions.md): the configured image of each
component without its digest, the tag as `version` (null for a tag that is not a release),
whether the selected Compose profiles enable the service, the Grafana origin and the RustFS
console origin when enabled, `features.backups` (the time of the newest Checkpoint in
`OB_BACKUP_DIR` when bootstrap ran; later Checkpoints do not update it) and
`features.alerts` (true for webhook or email with SMTP, false for the placeholder). It is
configuration, not observation: a version is what bootstrap configured, not what runs.
Rerun bootstrap after changing images, origins, profiles or alert delivery. The document
never contains secrets, digests, container names or host paths. A Checkpoint restore does
not rewrite it.

Liveness comes from `/health/<component>`, status only for clients outside `OB_OPERATOR_ALLOW`:

| Component | Probe |
| --- | --- |
| `caddy` | Caddy answers |
| `grafana`, `alloy` | `/api/health`, `/-/ready` |
| `loki`, `mimir`, `tempo` | `/ready` |
| `rustfs` | `/health/ready` with the `s3` profile; otherwise 502, and consumers never probe a disabled component |

Upgrading from the version 1 status timer: run `scripts/retire-status-timer.sh` as the
installation user, then `python3 scripts/bootstrap.py`. The script disables and removes
`observability-status.timer` and `.service` from the user's systemd directory, reloads the
user manager, and deletes `bootstrap.json`, `observer.lock` and the `status` directory under
the `OB_STATE_DIR` saved in `.env`. It prints each removal and is safe to rerun. Bootstrap
then replaces the version 1 `status.json`. `/versions.json` is gone; the Stack Console reads
`/status.json`.

## Image experiments

Every service accepts a complete `OB_*_IMAGE` reference from `.env` or the shell;
see `.env.example`. Empty or unset values inherit the digest-pinned defaults in
`compose.yaml`. `OB_RUSTFS_IMAGE` controls both RustFS and its init service.
Native `docker compose` applies the overrides directly. Tags and locally built images
are allowed for unvalidated experiments, including stores. Use a Checkpoint before a
persistent change; an image switch does not migrate data or change storage-mode rules.
Run bootstrap after changing references to refresh configured version labels. The console
reports the effective configuration at bootstrap time; it does not attest running content.
Shipped-default validation, smoke and recovery gates ignore installation and shell image
overrides. Renovate continues to update the inline defaults through its native Compose
manager and existing major/group policies. Checkpoint image requirements are in
[backup and restore](backup.md).
