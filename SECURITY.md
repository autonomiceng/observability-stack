# Security policy

## Scope

This repository configures upstream services. Use immutable image digests that pass the
Smoke Contract and review upstream security advisories before updating them.

## Reporting a vulnerability

Report privately through this repository's GitHub Security Advisories. If unavailable, use
the private contact route on the maintainer's profile. Include affected configuration,
reproduction and impact. Keep credentials, telemetry and exploit details out of public issues.

## Boundaries

Only Caddy publishes ports, bound to loopback by default. Grafana requires login and disables
anonymous access and signup. Loki, Mimir, Tempo and RustFS stay on the project's private
network. Backends have no authentication layer in this single-host deployment.

Alloy has privileged host access, host filesystem mounts and the Docker socket for collection.
A socket mounted read-only still permits Docker API mutations. Treat Alloy and all members of
`platform` as trusted host workloads; the internal Alloy HTTP/OTLP listeners are not public
APIs. The console, readiness routes and `/status.json` are unauthenticated and carry no
credentials. In public mode `/status.json` exposes to the internet each component's
configured image reference (without its digest), its version and the application origins;
see the [status document](docs/operations/maintenance.md#status-document).

Protect `.env` and volume backups. Grafana credentials initialize its database once; changing
`.env` later is not a password rotation. Logs and traces can contain sensitive application
data. Retention is not a secure-erasure guarantee for snapshots or backups. See the
[ingress](docs/operations/ingress.md) and [maintenance](docs/operations/maintenance.md) runbooks.
