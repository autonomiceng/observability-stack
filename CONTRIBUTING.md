# Contributing

Read [AGENTS.md](AGENTS.md), [CONTEXT.md](CONTEXT.md), [design](docs/DESIGN.md) and the
[binding ADR](docs/adr/0001-lgtm-on-one-host.md). Shared stack conventions live in
[docs/conventions.md](docs/conventions.md).

Toolchain is in `mise.toml`. Use Conventional Commits with `Co-Authored-By: Various Models`.
Keep changes focused and explain the problem and resulting behavior in the pull request.

```sh
scripts/validate.sh
python3 -m unittest discover -s tests
SMOKE_PROJECT=observability-smoke SMOKE_HTTP_PORT=18180 SMOKE_HTTPS_PORT=18543 scripts/smoke.sh
```

Validation checks both Compose storage configurations and pins, three Caddy modes, Alloy
with and without a token, ShellCheck and Python compilation. Smoke boots an isolated project
and checks Grafana provisioning through its authenticated APIs, Loki ingestion and Mimir's
self-scrape. Grafana has no general offline provisioning validator used here.

CI runs static gates and unit tests on pushes/PRs; full smoke runs on pull requests, weekly and on workflow
dispatch. An image bump requires a passing smoke run before merge. Report all failed or
unavailable checks. Scratch and plans remain untracked. Report vulnerabilities privately
through [SECURITY.md](SECURITY.md).

For file-specific validation and migration constraints, read the [change map](docs/agents/change-map.md).
