# observability-stack

Logs, metrics and traces for everything on your host, in one Grafana. Self-hosted, one Docker Compose file.

[![CI](https://github.com/autonomiceng/observability-stack/actions/workflows/ci.yml/badge.svg)](https://github.com/autonomiceng/observability-stack/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Grafana 13](https://img.shields.io/badge/Grafana-13-F46800)](https://github.com/grafana/grafana)
[![Alloy 1.19](https://img.shields.io/badge/Alloy-1.19-informational)](https://github.com/grafana/alloy)

- [What it is](#what-it-is)
- [Quick start](#quick-start)
- [Access modes](#access-modes)
- [What's inside](#whats-inside)
- [Upgrade](#upgrade)
- [Day two](#day-two)
- [The other stacks](#the-other-stacks)
- [Development](#development)
- [Security](#security)
- [License](#license)

## What it is

You run a few Docker stacks on one machine and want to see what they are doing without SSHing in and tailing logs. This stack runs the Grafana LGTM set: Loki for logs, Mimir for metrics, Tempo for traces, and Alloy as the one collector that feeds them.

Alloy discovers the host's containers (except disposable smoke and drill projects) and ships their logs with the Compose project and service as labels. Metrics from the sibling stacks are opt-in: `OB_SCRAPE_GATEWAY=true` for the LLM gateway, `OB_SCRAPE_EDGE=true` for Platform Edge after allowing the scraper address there, `OB_SCRAPE_BACKPLANE=true` plus `OB_BACKPLANE_OPERATIONS_TOKEN` for the backplane. All three are off by default, so this stack starts on its own. Its own services, the host filesystem and textfile metrics are always scraped. Grafana comes with the datasources wired, a starter dashboard and a few alert rules.

Everything stores to local volumes by default. An optional profile moves the backends to S3 on RustFS.

## Quick start

You need a Linux Docker host with journald, Docker Compose 2.24.4 or newer, and Python 3.11 or newer. [mise](https://mise.jdx.dev) installs the pinned tools if you use it. See [logging](docs/operations/logging.md) for hosts without journald.

```sh
git clone https://github.com/autonomiceng/observability-stack.git && cd observability-stack
python3 scripts/bootstrap.py
```

Bootstrap writes `.env` from `.env.example` with a generated Grafana admin password, creates or validates the shared `platform` network, starts everything and waits for it to be healthy. About a minute. `.env` is yours afterwards; bootstrap writes the values it derives to `data/derived.env`, so run Compose yourself as `docker compose --env-file .env --env-file data/derived.env ...` ([env files](docs/operations/maintenance.md#env-files)).

Alerts evaluate from the start but go nowhere until you configure delivery. Until then bootstrap reports `degraded` with `alert_delivery_placeholder` and the console shows it. Set `OB_ALERT_WEBHOOK_URL`, or `OB_ALERT_EMAIL` plus `OB_SMTP_URL`, in `.env` and run bootstrap again ([alerts](docs/operations/maintenance.md#alerts-and-metric-contracts)).

| URL | What |
| --- | --- |
| `http://localhost/` | Console: links, live health, alert delivery state, configured versions; `/status.json` is the same data for machines |
| `http://grafana.localhost/` | Grafana. User `admin`, password `OB_GRAFANA_ADMIN_PASSWORD` in `.env` |

Open Explore, pick Loki, and query `{compose_project="observability-stack"}`. Your own logs are already there. If the LLM gateway runs on the same host, its logs appear under `compose_project="llm-gateway-stack"`; for its metrics, set `OB_SCRAPE_GATEWAY=true` and add Alloy's Platform Network address to the gateway's `LG_CHECKPOINT_ALLOW` ([collection](docs/operations/logging.md#platform-edge-and-optional-collection)).

## Access modes

`OB_ACCESS_MODE` in `.env` selects how the stack is reached. Rerun `python3 scripts/bootstrap.py` after changing it. Details and every setting are in the [ingress runbook](docs/operations/ingress.md).

| You want | Settings | Read |
| --- | --- | --- |
| Localhost only (default) | `OB_ACCESS_MODE=local`; HTTP and self-signed HTTPS on loopback, no redirects | [Local Mode](docs/operations/ingress.md#local-mode-default) |
| Private access from your devices over Tailscale | Behind Platform Edge: its `bootstrap.py --tailscale --with observability`. Standalone: `OB_ACCESS_MODE=proxy`, `OB_GRAFANA_URL`, host `tailscale serve` | [Tailscale](docs/operations/ingress.md#tailscale) |
| Public hostname with Let's Encrypt | `OB_ACCESS_MODE=public`, `OB_PUBLIC_DOMAIN`, `OB_BIND_HOST=0.0.0.0` | [Public Mode](docs/operations/ingress.md#public-mode) |
| Corporate CA or certificate files | Not offered by this stack's Caddy; put it behind Platform Edge, which has `PE_TLS_ISSUER` | [Behind Platform Edge](docs/operations/ingress.md#behind-platform-edge) |
| Behind Platform Edge on a shared host | `OB_ACCESS_MODE=proxy`, `OB_HTTP_PORT=18180`; Edge's bundle installer writes these | [Behind Platform Edge](docs/operations/ingress.md#behind-platform-edge) |

## What's inside

| Service | Job | Data |
| --- | --- | --- |
| Caddy | The only published port. Routes by hostname, serves the console. | volume |
| Grafana | Dashboards, alerts, one login | volume |
| Alloy | Collects logs, scrapes metrics, receives OTLP traces | volume |
| Loki | Logs, 30 days | volume |
| Mimir | Metrics, 30 days | volume |
| Tempo | Traces, 7 days | volume |
| RustFS (optional, profile `s3`) | S3 backend for the three stores | volume |

Default images are pinned as `tag@sha256` in `compose.yaml`. Set a complete `OB_*_IMAGE` reference in `.env` for an experiment; see [image experiments](docs/operations/maintenance.md#image-experiments). Renovate opens the bump; a human merges it after the smoke test passes.

## Upgrade

```sh
scripts/backup.sh          # the rollback boundary
git pull
docker compose pull
python3 scripts/bootstrap.py
```

Bootstrap records any new setting, recreates what changed and waits for readiness. Read [maintenance](docs/operations/maintenance.md#updating-images) first: Tempo needs its query quiescence check before it stops, and `config.alloy` changes need an explicit Alloy recreate. An installation that ran the version 1 status timer retires it once with `scripts/retire-status-timer.sh` ([status document](docs/operations/maintenance.md#status-document)).

## Day two

- [Ingress and access modes](docs/operations/ingress.md)
- [Maintenance: version bumps, env files, alerts, status document](docs/operations/maintenance.md)
- [Runtime logs and collection](docs/operations/logging.md)
- [Backup, restore and the recovery drill](docs/operations/backup.md)
- [Host sizing and OOM recovery](docs/operations/capacity.md)
- [Disk-full recovery](docs/operations/disk-full.md)
- [Design](docs/DESIGN.md), [vocabulary](CONTEXT.md), [decisions](docs/adr/)

## The other stacks

This is one of four repos that deploy the same way and work together on one host:

- [llm-gateway-stack](https://github.com/autonomiceng/llm-gateway-stack): LiteLLM and Langfuse. Its logs arrive automatically; its metrics with `OB_SCRAPE_GATEWAY=true`.
- [agent-backplane](https://github.com/autonomiceng/agent-backplane): shared state, queues and approvals for agents. Scraped with `OB_SCRAPE_BACKPLANE=true`.
- [platform-edge](https://github.com/autonomiceng/platform-edge): one Caddy for ports 80 and 443 when more than one stack shares a host.

Each runs alone. Shared conventions are in [docs/conventions.md](docs/conventions.md).

## Development

```sh
scripts/validate.sh                    # static checks and image config validators
python3 -m unittest discover -s tests  # unit tests, no Docker
scripts/smoke.sh                       # disposable filesystem install
SMOKE_PROFILE=s3 scripts/smoke.sh      # RustFS restart persistence
scripts/backup-drill.sh                # disposable Checkpoint restore and measured RTO
```

CI runs validation and unit tests on every push and pull request, and both smoke profiles and both recovery drills on pull requests, weekly and on demand. See [CONTRIBUTING.md](CONTRIBUTING.md).

## Security

Report vulnerabilities through the [security policy](SECURITY.md). Alloy runs as root with the Docker socket to discover containers; treat the host it runs on as trusted.

## License

[MIT](LICENSE).

Third-party components: Grafana, Loki, Tempo and Mimir are licensed under AGPLv3;
Alloy is licensed under Apache-2.0. This repository's MIT license covers its own code
and configuration; bundled components retain their respective licenses.
