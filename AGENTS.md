# observability-stack

A self-hosted observability stack: Grafana for people and agents, Alloy for collection,
Loki for logs, Mimir for metrics, Tempo for traces, Caddy in front. One Compose project,
one host, durable local data.

Read before changing anything: `CONTEXT.md` (vocabulary), `docs/DESIGN.md` (the map),
`docs/adr/` (decisions and why). A change that contradicts an ADR is declared, never made quietly.

## Ways to hurt yourself

1. **Killing by pattern.** Never `pkill -f`, `pgrep | kill`, or kill a PID found by matching a name, path or worktree string. Your agent process has the worktree path in its argv. Kill only a PID captured at spawn, or the owner of your port from `ss -H -ltnp` after confirming `/proc/<pid>/cwd` is your worktree.
2. **Touching data you cannot rebuild.** Never delete installation volumes unless the user asked for data loss by name. Changing storage mode is a migration. Restore a Checkpoint when an upgrade changes a data format.
3. **Mutating the running stack to inspect it.** Read files and `docker compose config` to inspect. `up`, `down`, `restart` and `pull` need the user's authorization. Smoke owns only its fresh disposable project.
4. **Trusting the Docker socket mount.** Alloy runs as root with Docker's default capabilities and the Docker socket, which is host-root equivalent. A read-only socket mount does not limit Docker API operations. Keep the platform network trusted.

## Communication

Short, direct, precise, industry standard language. No "not X, but Y", no em-dashes.
State the result, then the evidence.

## Commits

- Conventional Commits: `<type>(scope): <description>`.
- Use `Co-Authored-By: Various Models`. Do not claim work performed by other models.
- Never commit `.env`, generated data, plans, research notes or agent scratch. Never print secrets.

## Documentation

Update `CONTEXT.md` when a term changes meaning. Add an ADR only for a decision that is hard
to reverse, surprising and a real trade-off. Most configuration changes need no new ADR.

## Delegation

For delegated work, use the model choices, risk paths and brief templates in
`docs/agents/model-routing.md`. At most two concurrent workers share this host's Docker daemon.

## Where things live

- `compose.yaml`: services, image pins and volumes. `compose.s3.yaml`: the storage mount override for the `s3` profile. `compose.proxy.yaml`: publishes HTTP only, selected by bootstrap in Proxy Mode.
- `.env.example`: every operator setting, prefixed `OB_`, one comment per assignment, no secrets.
- `scripts/bootstrap.py`: secrets, network, Compose file selection, derived values (`data/derived.env`, beside the env file), Grafana provisioning and file secrets under `OB_STATE_DIR`, readiness, and the Status Document (`OB_STATE_DIR/console/status.json`).
- `scripts/backup.sh`, `scripts/restore.sh`, `scripts/checkpoint.py`: fenced Checkpoints and restore. `scripts/backup-drill.sh`, `scripts/backup_drill.py`, `scripts/recovery_assertions.py`: the recovery drill. `scripts/destroy.sh`: deliberate removal. `scripts/retire-status-timer.sh`: one-time removal of the version 1 status timer.
- `scripts/validate.sh`: static gates. `scripts/smoke.sh`, `scripts/smoke_assertions.py`, `scripts/smoke_s3.py`: the Smoke Contract in both storage profiles.
- `config.alloy`: collection pipelines. `docker/{loki,mimir,tempo}/`: backend configuration, one file per storage mode.
- `docker/grafana/`: provisioned datasources, dashboard and alerts.
- `docker/caddy/`: one Caddyfile for the three access modes and the static Stack Console.
- `tests/`: Python unittest with a fake runner; no Docker calls.
- `docs/DESIGN.md` the map, `docs/adr/` decisions, `docs/operations/` runbooks, `docs/agents/` guidance, `docs/conventions.md` shared stack conventions (vendored from platform-edge; never edit it here).

Only Caddy publishes ports. Caddy, Grafana and Alloy join `platform`; backends stay private.
List each service's environment explicitly. Prefix stack settings with `OB_`. Service and
volume names are interfaces: renaming one needs a documented migration.

## Taste

Compose is the product. Keep logic in upstream apps and their configuration. Python 3
standard library and POSIX shell, no build step. Comments explain constraints. The console
updates when probes finish and has no continuous animation.

If a rule here fights the task, say so and get human sign-off before breaking it.

## Finish

Run `scripts/validate.sh`, `python3 -m unittest discover -s tests`, and `scripts/smoke.sh`
(both `SMOKE_PROFILE` values when storage is involved) for image, configuration, Alloy or
bootstrap changes; `scripts/backup-drill.sh` in both profiles for backup or recovery changes.
Report exact commands and counts, limitations and operator actions. A failed or skipped
gate is reported as such, never as a pass.
