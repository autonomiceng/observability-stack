# Maintenance

## Updating images

1. Read upstream release notes, including storage and config migrations. Before editing pins or configuration, run `scripts/backup.sh` with the checkout matching the running installation and retain the original `.env` separately. See [backup and restore](backup.md).
2. Resolve the stable tag with `docker buildx imagetools inspect <ref> --format '{{.Manifest.Digest}}'` and put the complete `tag@sha256` in `compose.yaml`.
3. Run `scripts/validate.sh`, unit tests and `scripts/smoke.sh`, `SMOKE_PROFILE=s3 scripts/smoke.sh` and `scripts/backup-drill.sh` against the new pins.
4. Verify the pre-change Checkpoint is complete and replicated off-host before applying the upgrade.
5. Apply with `docker compose pull` and `python3 scripts/bootstrap.py`, then check readiness and recent data in Grafana.

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
Compose commands cannot migrate stored data. Checkpoints include RustFS in S3 mode; the automated restore drill exercises filesystem mode. RustFS uses the
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
| Scrape target down | `up{job=~"llm-gateway(-valkey|-postgres|-checkpoints)?|backplane"} == bool 0` for 5m. `bool` returns 1 for a failed target into the > 0 threshold. An empty backplane token or `OB_SCRAPE_GATEWAY=false` removes that target. |

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
contract above. Configure
`OB_BACKPLANE_OPERATIONS_TOKEN` with the backplane's `BP_OPERATIONS_TOKEN` to enable scraping.
A configured token with a stopped backplane produces `up{job="backplane"}=0`; Alloy remains ready.

## Investigating missing data

Open Grafana Explore: `up{job="alloy"}` and `up{job="llm-gateway"}` in Mimir,
`{compose_project="llm-gateway-stack"}` in Loki. Request/spend series may be absent until a
request has completed; a successful scrape can still have no request activity. The starter
uses LiteLLM's request counter, total request latency histogram and spend counter. Labels
follow the gateway's exposed `model` series.

Tempo stays empty until a producer opts in. Check `/health/tempo` for readiness; successful
readiness alone does not prove a trace reached storage. The Smoke Contract does not yet
exercise OTLP ingestion or retention over multiple days.

## Resource failure

The six additional rules monitor host free bytes/inodes below 15%, Loki/Mimir/Tempo
ingestion failures, and Alloy remote-write backlog above 10,000 samples, each for five
minutes. Backend `/metrics` endpoints are scraped every 15 seconds. Inspect
`up{job=~"loki|mimir|tempo"}` before trusting those signals. No traffic may leave rejection
series absent; exporter failures and alert `NoData` require investigation.

See [capacity and OOM recovery](capacity.md), [disk-full recovery](disk-full.md) and
[Checkpoint recovery](backup.md). `docker compose down -v` preserves external volumes.
`scripts/destroy.sh` deletes them only after the operator types the project name.
