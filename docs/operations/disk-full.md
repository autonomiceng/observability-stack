# Disk full

Keep 30% headroom for compaction. Grafana warns after five minutes below 15% available
bytes or 15% free inodes on any writable host filesystem, including the Docker data
filesystem. Alloy's existing unix exporter reads host mount information from
`/rootfs/proc` and stats filesystems below `/rootfs`, mounted with slave propagation.
The filesystem scrape shares the `checkpoints` job with the textfile collector.
Container overlays and pseudo filesystems are excluded by the exporter's defaults.

Confirm the Docker data location and its host mount before relying on the alerts:

```sh
docker info --format '{{.DockerRootDir}}'
findmnt -T /var/lib/docker
df -h /var/lib/docker
df -i /var/lib/docker
```

Substitute Docker's reported path if different. In Grafana, check
`node_filesystem_avail_bytes`, `node_filesystem_size_bytes`, `node_filesystem_files_free`
and `node_filesystem_files` for that mountpoint. A Docker data directory on `/` uses
`mountpoint="/"`. Include separate mounts used by volumes and backups. An absent
filesystem series is a monitoring failure. See the
[unix exporter reference](https://grafana.com/docs/alloy/latest/reference/components/prometheus/prometheus.exporter.unix/).

During Loki's disk-full ingestion guard, `docker compose ps` stays green and the
Stack Console can still report healthy service probes. The two ingestion-failure signals
are the `loki-ingestion-failures` alert and an increasing
`loki_request_duration_seconds_count{route="loki_api_v1_push",status_code="500"}` counter.
Readiness alone does not show whether log writes are succeeding.

## What fails first

There is no fixed service order: the next allocation on the full filesystem fails.
Backend WAL appends, block writes and compaction fail; RustFS fails object writes in S3
mode. Alloy queues remote-write until its WAL cannot grow; log/OTLP retries are finite.
Grafana SQLite and Docker's own container log writer can also fail. Inode exhaustion
has the same effect even with free bytes. Retention deletion may need working space.

The `loki-ingestion-failures`, `mimir-ingestion-failures` and `tempo-ingestion-failures`
alerts watch five-minute rejection/write-failure rates above zero for five minutes.
`alloy-remote-write-backlog` watches more than 10,000 pending samples for five minutes.
Tempo also includes Alloy's documented `exporter_send_failed_spans_ratio_total`
([metric reference](https://grafana.com/docs/tempo/latest/troubleshooting/send-traces/alloy/)).
The ingestion rules return no data when the corresponding Backend scrape is unavailable.
Inspect the affected Backend logs and limits; rejections also occur without disk failure.
If Mimir or Grafana stops, these alerts cannot reliably evaluate. Use independent host
monitoring and configure real alert delivery; `OB_ALERTS=placeholder` provides no delivery.

## Safe cleanup and recovery

1. Pause upstream OTLP producers and high-volume application logging. Stop Alloy with
   `docker compose stop alloy` to prevent further ingestion while investigating.
2. Check host bytes/inodes, `docker system df`, mount health and Backend logs. Expand
   storage if possible. Move completed Checkpoints to verified off-host storage, then
   remove only specific superseded Checkpoint directories under your retention policy.
3. Remove only identified unused images or build cache after checking what other stacks
   need. Use normal host log rotation for host logs. Never truncate Docker's active log
   files manually. Never use `docker system prune --volumes` or `scripts/destroy.sh`
   for cleanup. Never remove Backend chunks, blocks, SQLite, WAL, indexes or Alloy's
   positions/WAL by hand. Shorter retention is asynchronous and may not rescue a full disk.
4. Restore at least 30% free bytes and sufficient inodes. If services exited, start
   RustFS first in S3 mode (`docker compose up -d --wait rustfs`), then
   `docker compose up -d loki mimir tempo grafana caddy`. Check all Backend readiness
   routes and logs. Allow WAL replay and compaction to finish before restarting Alloy.
5. Run `docker compose up -d --wait alloy`, resume producers gradually, and verify recent
   logs, metrics and traces. Watch pending samples fall and failure rates return to zero.
   Record the missing time range: rotated source logs, rejected samples and expired OTLP
   retries cannot be recreated by restarting the stack.

If stores cannot recover, retain the original volumes and restore a verified Checkpoint
into another empty project using [backup.md](backup.md). Do not overwrite the only copy.

## What stops first

Loki stops first. Its ingester watches the filesystem that holds its WAL and refuses
writes once usage passes 90 percent (`disk usage exceeded threshold, throttling writes`
in its log). Every push then fails with HTTP 500 and the message "Ingester is shutting
down", which is Loki's wording for this guard, not a crash. Alloy retries and finally
drops log batches. Mimir and Tempo keep writing until the disk is actually full. Free
space below the threshold and Loki resumes on its own; no restart is needed.
