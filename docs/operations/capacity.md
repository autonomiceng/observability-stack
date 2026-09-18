# Host capacity

Six long-lived containers run by default. S3 adds RustFS and a bucket initializer.
Reserve 4 CPU cores, 10 GiB available RAM for filesystem mode or 12 GiB for S3, and
40 GiB free SSD space, in addition to sibling stacks and the OS. Prefer 8 cores and
200 GiB SSD for sustained ingestion. Retention is a time horizon, not a disk quota.

## Memory envelope

Compose applies these hard limits and soft reservations on a single Docker host.
Each long-lived service also has a 512-process limit:

| Service | Reservation | Limit | Allowance |
| --- | --- | --- | --- |
| Caddy | 64 MiB | 256 MiB | Routing, TLS and concurrent requests |
| Grafana | 384 MiB | 1024 MiB | SQLite, alert evaluation and query responses |
| Alloy | 512 MiB | 1024 MiB | Docker discovery, cAdvisor, scrape buffers and remote-write WAL |
| Loki | 512 MiB | 1536 MiB | Active streams, WAL replay and two concurrent queries |
| Mimir | 1024 MiB | 2048 MiB | 100,000 active series, WAL replay and two concurrent queries |
| Tempo | 512 MiB | 1536 MiB | Trace buffers, WAL replay and compaction |
| RustFS (S3) | 512 MiB | 2048 MiB | Object writes, reads and background work |

The filesystem limits total 7424 MiB; S3 totals 9472 MiB. Reservations total 3008 MiB
and 3520 MiB respectively. Reservations are soft pressure thresholds, not measured usage.
These are **provisional budgets**, with 2–4 times the reservation as room for bursts.
No measured baseline was available in this slice: `docker stats --no-stream` failed
with Docker socket permission denied. Do not treat these as load-tested production limits.
The host verifier must measure steady ingestion, a representative query burst, compaction
and restart/WAL replay before accepting or adjusting them:

```sh
docker stats --no-stream --format '{{.Name}} {{.MemUsage}}'
```

Record peaks over those phases, workload rates and active series. Allow at least 30%
above measured peak, rerun both Smoke Contracts and the recovery drill, and preserve
host headroom for siblings. A single idle sample cannot size production.

Alloy's OTLP memory limiter precedes batching: hard limit 768 MiB, spike allowance
192 MiB, soft threshold 576 MiB, checked each second. It leaves 256 MiB below the
container cap for other work. It rejects OTLP under pressure; producers need retries.
It does not limit the Prometheus or Docker-log pipelines. See the
[Alloy limiter reference](https://grafana.com/docs/alloy/latest/reference/components/otelcol/otelcol.processor.memory_limiter/).

Loki accepts 4 MiB/s with an 8 MiB burst, 5,000 streams, 1 MiB/s per stream with a
2 MiB burst; queries allow two concurrent workers, parallelism two, 500 result series,
5,000 log entries and a one-minute timeout. WAL replay flushes at a 512 MB memory ceiling. Mimir accepts 10,000 samples/s with a
20,000 sample burst, 100,000 active series; queries allow two concurrent workers,
parallelism two, 100,000 fetched series, 200,000 chunks and a one-minute timeout.
Limits apply in both storage modes. Investigate cardinality before raising limits.
Configuration references: [Loki](https://grafana.com/docs/loki/latest/configure/) and
[Mimir](https://grafana.com/docs/mimir/latest/configure/configuration-parameters/).

## OOM recovery

Every long-lived service uses `restart: unless-stopped`. Docker restarts the affected
container after an OOM exit; it does not restart dependent services automatically.
Inspect `docker compose ps -a` and `docker inspect <container-id> --format '{{json .State}}'`
for `OOMKilled`. Repeated OOM needs reduced input/query load or a measured limit increase.

| Victim | Recovery and possible loss |
| --- | --- |
| Caddy | Connections fail; container restarts, durable backend data remains. |
| Grafana | Queries and alert evaluations pause. SQLite recovers from its journal; in-flight writes may fail. |
| Alloy | Positions and remote-write WAL replay from its volume. In-memory OTLP batches and unsent log batches may be lost. Source log rotation and retry exhaustion can create gaps. |
| Loki / Mimir / Tempo | Backend restarts and replays durable WAL/blocks. Queries fail during replay; unacknowledged and unflushed writes can be lost. Never delete WAL to fix a restart loop. |
| RustFS | All S3 Backends can fail writes/queries until RustFS recovers. Local WAL survives; buffered requests require retries. |

Reduce producers first, recover storage (RustFS if used, then Backends), verify readiness,
then resume Alloy and queries. If WAL replay itself exceeds the cap, temporarily increase
that service's limit within host capacity. A crash loop is not evidence of corrupt data.

## Disk sizing

Loki and Mimir retain 30 days; Tempo retains seven. Measure daily compressed growth times
retention, plus WALs, indexes and temporary compaction space. Keep at least 30% free.
Docker logs rotate at 10 MB times three files per container in this project. Other
projects control their own log rotation. Monitor both bytes and inodes, ingestion
failures and Alloy's remote-write backlog; follow [disk-full.md](disk-full.md).
Reserve at least the full persisted volume size per uncompressed Checkpoint, plus
space for a restore. A local RustFS volume shares the host failure domain. Replicate
Checkpoints off-host as described in [backup.md](backup.md).
