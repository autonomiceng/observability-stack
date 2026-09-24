# observability-stack

Logs, metrics and traces for everything on your host, in one Grafana. Self-hosted, one Docker Compose file.

[![CI](https://github.com/autonomiceng/observability-stack/actions/workflows/ci.yml/badge.svg)](https://github.com/autonomiceng/observability-stack/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Grafana 13](https://img.shields.io/badge/Grafana-13-F46800)](https://github.com/grafana/grafana)
[![Alloy 1.19](https://img.shields.io/badge/Alloy-1.19-informational)](https://github.com/grafana/alloy)

## What it is

You run a few Docker stacks on one machine and want to see what they are doing without SSHing in and tailing logs. This stack runs the Grafana LGTM set: Loki for logs, Mimir for metrics, Tempo for traces, and Alloy as the one collector that feeds them.

Alloy discovers host containers, excluding disposable smoke/drill Compose logs and ships its logs with the Compose project and service as labels. Collect gateway metrics with `OB_SCRAPE_GATEWAY=true`, Edge metrics with `OB_SCRAPE_EDGE=true` after allowing the scraper address at Edge, or backplane metrics with `OB_SCRAPE_BACKPLANE=true` and `OB_BACKPLANE_OPERATIONS_TOKEN` set to its operations token. All three are off by default, so this stack starts on its own. Self, container, host filesystem, textfile and Backend metrics are always scraped. Grafana comes with the datasources wired, a starter dashboard and a few alert rules.

Everything stores to local volumes by default. An optional profile moves the backends to S3 on RustFS.

## Quick start

You need a Linux Docker host with journald, Docker Compose 2.24.4 or newer, and Python 3.11 or newer. [mise](https://mise.jdx.dev) installs the pinned tools if you use it. See [logging](docs/operations/logging.md) for host prerequisites and portability.

```sh
git clone https://github.com/autonomiceng/observability-stack.git && cd observability-stack
python3 scripts/bootstrap.py
```

Bootstrap writes `.env` from `.env.example` with a generated Grafana admin password, creates or validates the shared `platform` network allocation, starts everything and waits for it to be healthy. About a minute.

Alerts evaluate from the start but go nowhere until you configure delivery. Until then bootstrap reports `degraded` with `alert_delivery_placeholder` and the console shows it. Set `OB_ALERT_WEBHOOK_URL`, or `OB_ALERT_EMAIL` plus `OB_SMTP_URL`, in `.env` and run bootstrap again; see [alert setup](docs/operations/maintenance.md#alerts-and-metric-contracts).

| URL | What |
| --- | --- |
| `http://localhost/` | Console: links, live health, configured versions; `/status.json` is the same data for machines |
| `http://grafana.localhost/` | Grafana. User `admin`, password in `.env` |

Open Explore, pick Loki, and query `{compose_project="observability-stack"}`. Your own logs are already there.

If the LLM gateway runs on the same host, its logs appear automatically under `compose_project="llm-gateway-stack"`. For metrics, set `OB_SCRAPE_GATEWAY=true` and add Alloy’s Platform Network address to the gateway’s `LG_CHECKPOINT_ALLOW`. See [logging and collection](docs/operations/logging.md).

Local mode offers HTTP and self-signed HTTPS. It does not force HTTP visitors onto HTTPS. To put it on the internet, set `OB_ACCESS_MODE=public`, a domain and a public bind address in `.env`. When Platform Edge handles HTTPS for this stack, select `proxy`; behind Edge also set `OB_HTTP_PORT=18180`, and the default `OB_TRUSTED_PROXIES=172.30.0.2/32` already trusts Edge's reserved address. See [ingress](docs/operations/ingress.md).

## What's inside

| Service | Job | Data |
| --- | --- | --- |
| Caddy | The only published port. Routes by hostname, serves the console. | volume |
| Grafana | Dashboards, alerts, one login | volume |
| Alloy | Collects logs, scrapes metrics, receives OTLP traces | volume |
| Loki | Logs, 30 days | volume |
| Mimir | Metrics, 30 days | volume |
| Tempo | Traces, 7 days | volume |
| RustFS (optional) | S3 backend for the three stores | volume |

Default images are pinned as `tag@sha256` in `compose.yaml`.
Set a complete `OB_*_IMAGE` reference in `.env` for an operator experiment;
see [image experiments](docs/operations/maintenance.md#image-experiments). Renovate opens the bump; a human merges it after the smoke test passes.

## Built on

| Project | Stars | What we use it for |
| --- | --- | --- |
| [Grafana](https://github.com/grafana/grafana) | ![stars](https://img.shields.io/github/stars/grafana/grafana?style=flat) | Dashboards and alerting |
| [Alloy](https://github.com/grafana/alloy) | ![stars](https://img.shields.io/github/stars/grafana/alloy?style=flat) | The collector |
| [Loki](https://github.com/grafana/loki) | ![stars](https://img.shields.io/github/stars/grafana/loki?style=flat) | Log storage and search |
| [Mimir](https://github.com/grafana/mimir) | ![stars](https://img.shields.io/github/stars/grafana/mimir?style=flat) | Metrics storage |
| [Tempo](https://github.com/grafana/tempo) | ![stars](https://img.shields.io/github/stars/grafana/tempo?style=flat) | Trace storage |
| [Caddy](https://github.com/caddyserver/caddy) | ![stars](https://img.shields.io/github/stars/caddyserver/caddy?style=flat) | Ingress and automatic HTTPS |
| [RustFS](https://github.com/rustfs/rustfs) | ![stars](https://img.shields.io/github/stars/rustfs/rustfs?style=flat) | Optional S3 backend |
| [Docker Compose](https://github.com/docker/compose) | ![stars](https://img.shields.io/github/stars/docker/compose?style=flat) | Running it all |

## The other stacks

This is one of four repos that deploy the same way and work together on one host:

- [llm-gateway-stack](https://github.com/autonomiceng/llm-gateway-stack): LiteLLM and Langfuse. Sends its logs and metrics here.
- [agent-backplane](https://github.com/autonomiceng/agent-backplane): shared state, queues and approvals for agents. Scraped here when `OB_SCRAPE_BACKPLANE=true`.
- [platform-edge](https://github.com/autonomiceng/platform-edge): one Caddy for ports 80 and 443 when more than one stack shares a host.

Each runs alone. Shared conventions are in [docs/conventions.md](docs/conventions.md).

## Day two

- [Ingress and access modes](docs/operations/ingress.md)
- [Status document](docs/operations/maintenance.md#status-document)
- [Maintenance and version bumps](docs/operations/maintenance.md)
- [Host sizing and OOM recovery](docs/operations/capacity.md)
- [Disk-full recovery](docs/operations/disk-full.md)
- [Backup, restore and recovery drill](docs/operations/backup.md)
- [Design](docs/DESIGN.md), [vocabulary](CONTEXT.md), [decisions](docs/adr/)

Alert rules that need a metric no stack exposes yet are marked pending in the alert file rather than silently never firing.

## Development

```sh
scripts/validate.sh                    # static checks and image config validators
python3 -m unittest discover -s tests  # unit tests, no Docker
scripts/smoke.sh                       # disposable filesystem install
SMOKE_PROFILE=s3 scripts/smoke.sh      # RustFS restart persistence
scripts/backup-drill.sh                # disposable Checkpoint restore and measured RTO
```

CI runs validation and unit tests on pull requests, pushes to `main`/`develop`, weekly schedules and manual dispatch. Both filesystem and S3 smoke modes run on pull requests, weekly and on manual dispatch. See [CONTRIBUTING.md](CONTRIBUTING.md).

## Security

Report vulnerabilities through the [security policy](SECURITY.md). Alloy reads the Docker socket to discover containers; treat the host it runs on as trusted.

## License

[MIT](LICENSE).

Third-party components: Grafana, Loki, Tempo and Mimir are licensed under AGPLv3;
Alloy is licensed under Apache-2.0. This repository's MIT license covers its own code
and configuration; bundled components retain their respective licenses.
