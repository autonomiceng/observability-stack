# Backup and restore

```sh
scripts/backup.sh
scripts/restore.sh /mnt/backups/20260917T020000000000Z
```

Both accept `--env-file /path/to/.env`. Set `OB_BACKUP_DIR` to an existing, mounted
backup directory. Verify the expected mount with `findmnt` before capture. The scripts
cannot detect an unmounted disk hidden by its empty mountpoint. Use encrypted storage
and replicate off-host over encrypted transport. Scheduling, encryption and replication are the operator's responsibility.
`OB_BACKUP_KEEP` defaults to 7 complete Checkpoint sets. A capture followed by healthy service resumption prunes older
complete timestamp directories; incomplete captures remain for inspection. Before fencing,
backup refuses when free space on the backup filesystem is below the size of the last
complete Checkpoint. The first capture has no prior size estimate; allow space for every
volume plus configuration, and account for growth between captures.

## Checkpoint contents and fence

Each UTC timestamp directory is mode 0700 and contains:

- `manifest.json`: completion time, full image pins, environment key names, storage mode,
  fence status, byte sizes and SHA-256 checksums. The manifest is published last, after archive-member validation.
- `grafana-data.tar`, `loki-data.tar`, `mimir-data.tar`, `tempo-data.tar`, `alloy-data.tar`:
  complete, uncompressed archives preserving volume ownership. Grafana SQLite and its
  journal are copied consistently with Grafana stopped.
- `rustfs-data.tar` in S3 mode: complete local object storage, captured after its writers stop.
- `installation/`: the storage-mode marker from `OB_STATE_DIR`.
- `configuration/`: both Compose files, Alloy config and `docker/` provisioning/configuration.

Keep the **original `.env` separately** in protected configuration backup or a password
manager, and retain the matching checkout. The manifest records keys only; the scripts
never copy env values into the Checkpoint. Manifest v1 cannot attest that supplied secret
values match those used at capture. Checks that all managed keys exist and that shell values
agree with the supplied file do not establish historical identity. Restore warns about
captured key names absent from the supplied file, without displaying values; it cannot detect
changed values under retained names. Supplying the original secrets remains an operator
requirement. Readiness alone does not verify credentials or decryptability. Verify Grafana
login and any credential-dependent integrations after restore. The drill authenticates its
historical dashboard query with the preserved original Grafana password; S3 inventories also
use the preserved access credentials. It does not attest every external integration secret.

Volume data can contain credentials and sensitive telemetry, so the entire set requires
protection. Caddy certificate/config
volumes are regenerated during restore; Public Mode recovery needs DNS/TLS access again.
Generated console metadata, Grafana provisioning/SMTP configuration and textfile metrics
are rebuilt from the matching checkout and original env, not archived.

Pause direct producers before backup. The default fence stops Caddy, Alloy, Grafana,
Loki, Mimir and Tempo in that order, then RustFS in S3 mode, with 120 seconds per service.
Before stopping anything, backup verifies the live image pins and all volume/bind mounts
against resolved Compose, requires every source volume to exist, and rejects other running
consumers of those volumes. Volume existence and consumers are checked again under the
fence before archiving and before publishing the manifest. Every stop must reach exit 0 without `OOMKilled`; forced
kills, missing containers and unsuccessful exits abort before a completed manifest.
`--stop-timeout SECONDS` increases the grace period for larger stores. Do not run Compose
changes or independent writers during capture. Env and repository locks exclude concurrent
bootstrap and Checkpoint operations through these scripts, not direct Docker commands.

The script attempts to resume all fenced services on success, failure or catchable interruption.
It waits for Compose health and all five HTTP readiness probes before publishing the success
timestamp or pruning older Checkpoints. A resumption failure preserves the previous timestamp
and retention set; a completed manifest remains a usable recovery artifact. If capture and
resumption both fail, the capture error remains primary and a separate sanitized diagnostic
reports the resumption failure. Repeated TERM/HUP/INT signals are ignored during resumption;
the previous handlers are restored afterward. SIGKILL or host loss prevents cleanup: inspect the incomplete directory and restart storage, then the
Collector and ingress manually. A directory without a manifest is incomplete. Remove it
only after confirming no capture is running. Checksums detect corruption, not malicious
replacement. Backups take the stack offline for the duration of the volume copies.
Producer retries, source log rotation and in-memory Collector buffers bound capture-time
telemetry loss; the fence guarantees consistent persisted storage, not lossless producers.

## Restore into an empty project

Volumes are external and named `${OB_VOLUME_PREFIX}_<role>`; the default prefix is
`observability-stack`, preserving existing default installation names. Bootstrap creates
missing volumes. `docker compose down -v` preserves them. Deliberate destruction uses
`scripts/destroy.sh --env-file /path/to/.env`, which lists the volumes and requires typing
the exact Compose project name. It preserves host files, including secrets and Checkpoints.
Smoke and the drill set the prefix to their disposable project and explicitly remove only
those volumes. Restore never deletes destination data to make room.

Existing installations with a custom Compose project must set `OB_VOLUME_PREFIX` to their
existing volume prefix before bootstrap. Changing a prefix selects different storage;
verify names with `docker volume ls` before proceeding.

1. Fence the old installation and its producers. Keep its volumes and Checkpoint intact.
   Obtain the original `.env` and matching config checkout. Do not run bootstrap first.
2. Select a fresh `COMPOSE_PROJECT_NAME` and `OB_VOLUME_PREFIX`, isolated `OB_PLATFORM_NETWORK`, free ports and
   an empty `OB_STATE_DIR`. Preserve the original secret values and storage profile.
   Update `OB_PUBLIC_PORT_SUFFIX` for the recovery endpoint. Provide an existing mounted
   `OB_BACKUP_DIR`; it may point to the source repository.
3. Run `scripts/restore.sh <Checkpoint> --env-file <original-env>`. Restore verifies the
   manifest, hashes, archive members, image pins and matching configuration before writes.
   It refuses running project containers, any running container consuming a target volume,
   any non-empty project volume (including Caddy), and a non-empty installation marker directory.
   Empty existing volumes are allowed. Archive members must be regular files or directories
   with relative paths and no `..` components; links and special files are rejected during
   both capture and restore. Alert delivery settings and shared-network availability are
   checked before extracting archives or writing the installation marker.
4. It restores every data volume and the marker before starting any service. It boots with
   Compose health checks and probes all five HTTP endpoints.
   Verify historical queries and Grafana login before routing producers to the new stack.

Failed restore leaves partial storage for diagnosis. Use another empty destination after
fixing the cause. If the final startup fails, some services may already be running.
An empty Grafana health response does not prove historical telemetry exists; query known
pre-Checkpoint markers or production time ranges before declaring recovery complete.

Bootstrap refuses `storage_mode_unknown` when project volumes exist without the marker,
even if all secrets are present. Recover the marker from the Checkpoint. For a separately
verified legacy installation, explicitly record the prior mode before bootstrap:

```sh
mkdir -p /path/to/state/installation
printf '%s\n' filesystem > /path/to/state/installation/storage-mode
```

Use `s3` only if the existing data was written in S3 mode. This records known history;
it does not migrate data. Scratch `--render-only --env-file /tmp/...` remains an offline
env rendering operation and never starts or creates installation state.

## RPO, RTO and scheduling

RPO is the successful **Checkpoint interval**, including off-host replication. Daily
Checkpoints imply up to 24 hours of loss. Failed capture or replication invalidates that
bound. There is no continuous telemetry archive or point-in-time recovery here.

RTO is measured by the drill from disposable teardown through restore and historical
marker verification. It excludes capture, off-host fetch, Docker installation and image
pulls. No measured RTO is claimed until the host drill passes. Measure again with
representative volume sizes; archive extraction and WAL replay can dominate recovery.

```sh
SMOKE_PROFILE=filesystem SMOKE_PROJECT=observability-drill SMOKE_HTTP_PORT=18190 scripts/backup-drill.sh
SMOKE_PROFILE=s3 SMOKE_PROJECT=observability-drill-s3 SMOKE_HTTP_PORT=18190 scripts/backup-drill.sh
```

The drill accepts only `SMOKE_PROFILE=filesystem` (default) or `s3`; ambient installation
profile settings are discarded. CI runs both recovery profiles with a 60-minute timeout
per job. Require both `Recovery contract` jobs in release/branch protection alongside smoke.
A workflow alone does not configure required status checks in repository settings.

It refuses existing disposable resources, makes a temporary checkout copy, and boots an
isolated project. Only this copy disables Docker log discovery, cAdvisor and host filesystem
collection, removes the Collector's host mounts and drops privileged mode. Log markers are
injected directly into Loki; a temporary textfile metric and one OTLP trace exercise Alloy.
The production Smoke Contract still requires Docker log ingestion and host filesystem metrics.
Other host collectors may observe the drill's containers or resource usage, so use a dedicated
Docker host when strict host isolation is required. Drill containers retain the disposable
Compose labels excluded by this stack's production log collection rules.

Before capture the drill verifies its unique log, metric, trace and API-created Grafana
dashboard. Producers are removed before capture. In S3 mode it flushes Loki/Mimir, requires
nonempty object inventories and hashes their object bodies with SHA-256. After capture it
changes the dashboard, destroys all disposable volumes (including RustFS) and the installation
marker, restores into empty storage, and verifies the original time-bounded telemetry,
dashboard contents, exclusion of the later dashboard edit, and the saved S3 object hashes.
It prints `RESTORE DRILL PASSED (<profile>)` and measured `RTO=...s` only after these checks.
Backup and restore subprocesses inherit only the Checkpoint CLI's sanitized failure diagnostics;
raw Docker command stderr remains captured. Failed artifacts stay in the printed temporary path; they include generated credentials and
sensitive volume contents. The successful drill removes its temporary copy and resources.

These checks cover historical trace recovery across the complete local/WAL/object-store
Checkpoint. They do not independently establish that Tempo had flushed a trace object to S3,
or test recovery from an incomplete combination of local WALs and object storage. Mimir ruler
and Alertmanager state, every Grafana setting, and exclusion of post-Checkpoint telemetry
writes are not exercised. The S3 Smoke Contract remains a separate object-store restart test.
Neither profile has a recovery pass until the supported drill succeeds on the release candidate.

Run monthly and after backup/pin changes. Example daily capture:

```cron
0 2 * * * cd /opt/observability-stack && scripts/backup.sh >> /var/log/observability-backup.log 2>&1
```

After success the script atomically replaces `OB_STATE_DIR/textfile/checkpoint.prom` with
`stack_checkpoint_last_success_timestamp_seconds{stack="<project>"}`. Failure preserves
the prior timestamp. Grafana's Checkpoint alert fires after 26 hours; no first successful
backup produces `NoData`. Monitor backup exit status, newest complete manifest age,
missing/unwritable backup mount, off-host replication lag and failed drills independently.
Grafana and Mimir are in the same failure domain and cannot report their own total outage.

### Tempo query quiescence

The pinned Tempo 3.0.3 query frontend removes empty tenant queues every 30 seconds.
Stopping immediately after a query can strand its shutdown wait because the cleanup
loop has stopped. Capture fences ingress and Grafana first, then allows 35 seconds
for that cleanup before stopping Tempo. It still requires exit zero. Do not query
Tempo directly from another container while the checkpoint fence is active.
Ordinary service stops have a 45-second grace period for the backend worker;
a recent query can still require a forced stop outside the coordinated checkpoint
procedure. Use the checkpoint procedure before upgrades and preserve its source
volumes until recovery verification completes.

Upstream source: [frontend queue lifecycle](https://github.com/grafana/tempo/blob/v3.0.3/modules/frontend/queue/queue.go).
