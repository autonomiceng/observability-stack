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

- `manifest.json`: completion time, effective immutable image references by service, environment
  key names, storage mode, fence status, byte sizes and SHA-256 checksums. The manifest is published last, after archive-member validation.
- `grafana-data.tar`, `loki-data.tar`, `mimir-data.tar`, `tempo-data.tar`, `alloy-data.tar`:
  complete, uncompressed archives preserving volume ownership. Grafana SQLite and its
  journal are copied consistently with Grafana stopped.
- `rustfs-data.tar` in S3 mode: complete local object storage, captured after its writers stop.
- `installation/`: the storage-mode marker from `OB_STATE_DIR`.
- `configuration/`: both Compose files, Alloy config and `docker/` provisioning/configuration.

Keep the **original `.env` separately** in protected configuration backup or a password
manager, and retain the matching checkout. The manifest records environment key names; the scripts
never copy secret values from the env file into the Checkpoint. Manifests cannot attest that supplied secret
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

Archive validation accepts only regular files and directories. An archived hardlink, symlink,
FIFO, device or other special-file entry produces `FAIL: unsupported archive member`.
Capture then aborts without a completed manifest and attempts to resume writers. This
restriction also applies to restore; the live drills do not establish compatibility with
all aged production volume layouts. Inspect the incomplete archive privately to identify
the entry. Do not delete or flatten production files to force validation to pass; retain
the source volumes and use a backup procedure that preserves the required file semantics.

Pause direct producers before backup. The default fence stops Caddy, Alloy, Grafana,
Loki, Mimir and Tempo in that order, then RustFS in S3 mode, with 120 seconds per service.
Before stopping anything, backup resolves every effective image locally and verifies running
content IDs and all volume/bind mounts against resolved Compose, requires every source volume
to exist, and rejects other running consumers of those volumes. Tag-only references require
a locally available RepoDigest whose inspected content ID
matches the configured image and running container. Capture prefers the configured
repository when several digests exist; otherwise retain access to the registry recorded
in the manifest. Local-only images without a verified
RepoDigest are refused before fencing or creating a capture directory. Helpers use the verified
Caddy content ID with pulling disabled. Image resolution and helpers never pull. A mutable tag alone
cannot reproduce a Checkpoint. Preserve access to the captured digest references in a registry
or a protected image archive, and load/pull those exact references before recovery.

Volume existence and consumers are checked again under the
fence before archiving and before publishing the manifest. Every stop must reach exit 0 without `OOMKilled`; forced
kills, missing containers and unsuccessful exits abort before a completed manifest.
`--stop-timeout SECONDS` sets each service's stop grace and must be a positive integer.
Tempo quiescence has a separate budget of `max(120, SECONDS)` seconds; a short stop grace
never shortens its 35-second quiet window. Do not run Compose
changes or independent writers during capture. Env and repository locks exclude concurrent
bootstrap and Checkpoint operations through these scripts, not direct Docker commands.

The script attempts to resume all fenced services on success, failure or catchable interruption.
After a successful capture, resumption has a 120-second budget. After failure or interruption,
it adds a settle window of `S = --stop-timeout + 10` seconds to watch for late stops, even
when writers initially appear healthy. It retries transient Docker inspection failures and
restarts stopped writers. Missing writers are recreated
from the resolved Compose configuration. An interrupted Compose stop can finish on the daemon
after its CLI exits, so resumption rechecks container state after HTTP probes and retries late
stops. Permanent failures exhaust the deadline and fail capture's overall result.
Every writer must be running, and every configured Docker healthcheck must be healthy,
including RustFS in S3 mode. Distroless Backends without Docker healthchecks require running
state plus HTTP readiness. All five HTTP probes must pass before publishing the success
timestamp or pruning older Checkpoints. An in-flight HTTP probe can extend the final deadline
by up to approximately 8 seconds (5-second request timeout plus 3-second retry sleep).
The shared deadline is checked before each probe; this overrun is not multiplied by five.

A resumption failure preserves the previous timestamp and retention set. A completed manifest
remains a usable recovery artifact; the failure diagnostic prints its path. If capture and
resumption both fail, the capture error remains primary and a separate sanitized diagnostic
reports the resumption failure. Repeated TERM/HUP/INT signals are ignored during resumption;
the previous handlers are restored afterward. Resume commands use separate process sessions
so terminal process-group signals cannot cancel them.

Archive helpers have unique names and ownership labels. Creation completes before honoring
an interruption; cleanup verifies ownership and removes the helper by container ID before
resuming services. An unknown preexisting container is never removed. A cleanup failure is
fatal to quiescence, which creates no further helpers. The original helper command error
is retained in the diagnostic and exception chain when cleanup also fails. A deferred
interruption still aborts after cleanup is attempted, even when the command failed.
Inspect any surviving helper privately before starting another capture.

For a systemd backup service, set `KillMode=mixed`: its initial SIGTERM reaches the main
Checkpoint process, allowing cleanup and resumption. Size `TimeoutStopSec` from the time
SIGTERM is sent, including deferred helper creation/cleanup and the full failure-resumption
allowance. With stop grace `t`, the shared resumption deadline is `120 + S` seconds,
where `S = t + 10`: **250 seconds at the default `t=120`**. Initial startup and polling
consume that same budget. Time spent starting services also counts toward the settle
window, so slow startup does not restart that window or exhaust the polling budget twice.
Allow more than 250 seconds at defaults, plus the final HTTP overrun, helper/control-operation
time and scheduling margin. The daemon's late stop overlaps the settle window; do not add
another full service stop grace to this calculation.

There is no finite guaranteed wall-clock maximum: helper creation and ownership-checked
cleanup wait for Docker, and these control operations can stall. The complete capture also
includes six sequential service stop graces (seven in S3), quiescence of `max(120, t)`, and
unbounded archive/fsync time before resumption. At defaults, the configured fence plus
worst-case failure resumption sums to 1,090 seconds in filesystem mode or 1,210 seconds in S3,
excluding copies, control operations and HTTP overrun. These whole-run allowances are
separate from systemd's post-SIGTERM `TimeoutStopSec`.
A separate process session does not escape a systemd cgroup. `KillMode=control-group`, explicit
cgroup-wide signals, and systemd's final SIGKILL can still terminate resume children.
SIGKILL or host loss prevents cleanup: inspect the incomplete directory and any owned helper,
then restart storage, the Collector and ingress manually. A directory without a manifest is
incomplete. Remove it only after confirming no capture or helper is running. Checksums detect
corruption, not malicious replacement. Backups take the stack offline for the volume copies.
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

Manifest v2 records active services and their immutable references. Restore resolves the supplied
env's effective images locally and compares immutable references before checking destination
volumes or writing data. A tag that moved is refused; set the corresponding `OB_*_IMAGE` to
the captured reference. Restore and capture resumption pin newly created services to the verified
references for that invocation. Retain those overrides in `.env` for subsequent native Compose
operations. Original secrets, storage mode and configuration must still match.
Legacy v1 manifests remain accepted with their original byte-identical configuration checkout
and shipped pin list; effective active images must resolve to those same immutable references.
The newer Checkpoint scripts can be used with that checkout. Neither manifest version attests
historical secret values.

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
Both profiles have passed historical-trace recovery with exit-zero fences. Require fresh
passes from the supported drills on each release candidate.

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
loop has stopped. After fencing Caddy, Alloy and Grafana and stopping Loki and Mimir,
capture polls Tempo's `tempo_query_frontend_queue_length` and
`tempo_query_frontend_connected_clients` metrics. Every tenant queue sample must be zero,
and connected clients must be positive. Queue samples can be absent before the first query;
the connected-clients sample must still confirm the live frontend.

The queue must remain empty across successful observations for 35 seconds before Tempo
is stopped. Polls are one second apart, plus probe overhead. A busy queue, missing/invalid
frontend metrics or failed fetch resets that window. The overall quiescence budget is
`max(120, --stop-timeout)` seconds, separate from the subsequent stop grace. Cleanup failures
and interruptions abort immediately after cleanup is attempted instead of retrying probes.
Expiry aborts before stopping Tempo or archiving and attempts to resume earlier services.
Tempo must still stop with exit zero and without OOM. Queue length does not measure
executing queries, and sampling cannot exclude activity between probes. Keep direct query
clients paused throughout the fence; this check is not a guarantee that all queries finished.

Tempo is distroless. Each probe uses the pinned Caddy helper image with `wget` in the network
namespace of a running Tempo container whose project/service labels have been verified.
It reads `127.0.0.1:3200/metrics` inside that namespace, including with a remote Docker daemon.
The helper retains its unique ownership label and is removed before the next probe. Its
attached command receives the remaining quiescence budget; creation and ownership-checked
cleanup are allowed to finish so expiry does not abandon a helper. A stalled Docker control
operation can therefore extend elapsed time beyond the metrics budget.

For manual maintenance, pause direct query clients and stop Caddy, Alloy and Grafana first,
then run the same bounded check from the checkout, using the installation's env file:

```sh
PYTHONPATH=scripts python3 -c 'from pathlib import Path; from checkpoint import Stack; Stack(Path(".env")).quiesce_tempo()'
```

Only after it succeeds, stop Tempo with `docker compose stop -t 120 tempo` and require
exit zero with `OOMKilled=false`. On failure, keep Tempo running, investigate privately,
and resume the fenced query sources if abandoning maintenance. Ordinary service stops
have a 45-second grace period for the backend worker; a recent query can still require
a forced stop outside this procedure. Preserve source volumes until recovery verification
completes.

Upstream sources: [frontend metrics](https://github.com/grafana/tempo/blob/v3.0.3/modules/frontend/v1/frontend.go)
and [queue lifecycle](https://github.com/grafana/tempo/blob/v3.0.3/modules/frontend/queue/queue.go).
