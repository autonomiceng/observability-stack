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
4. **Trusting the Docker socket mount.** Alloy has host-root access for container discovery and metrics. A read-only socket mount does not limit Docker API operations. Keep the platform network trusted.

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

- `compose.yaml`: services, image versions and volumes. `compose.s3.yaml`: storage mount override.
- `.env.example`: operator settings. `scripts/bootstrap.py`: secrets, network and readiness.
- `config.alloy`: collection pipelines. `docker/{loki,mimir,tempo}/`: backend configuration.
- `docker/grafana/`: provisioned datasources, dashboard and alerts.
- `docker/caddy/`: one Caddyfile for both access modes and the static Stack Console.
- `scripts/validate.sh`, `scripts/smoke.sh`: validation and the Smoke Contract.
- `tests/`: Python unittest with a fake runner; no Docker calls.
- `docs/operations/`: runbooks. `docs/conventions.md`: shared stack conventions.

Only Caddy publishes ports. Caddy, Grafana and Alloy join `platform`; backends stay private.
List each service's environment explicitly. Prefix stack settings with `OB_`. Service and
volume names are interfaces: renaming one needs a documented migration.

## Taste

Compose is the product. Keep logic in upstream apps and their configuration. Python 3
standard library and POSIX shell, no build step. Comments explain constraints. The console
updates when probes finish and has no continuous animation.

If a rule here fights the task, say so and get human sign-off before breaking it.

## Finish

Run `scripts/validate.sh`, `python3 -m unittest discover -s tests`, and `scripts/smoke.sh` for
image, config or bootstrap changes. Report exact commands and results, limitations and
operator actions. A failed gate is a failed gate.
