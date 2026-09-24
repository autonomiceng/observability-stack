# Observability Stack

The terms this repository uses, one sentence each. Use these words in code, docs and
commits; avoid the listed alternatives.

**Collector**: the component (Alloy) that discovers producers and routes their logs,
metrics and traces to a Backend.
_Avoid_: agent, shipper

**Backend**: a durable store and query interface for one telemetry signal: Loki for logs,
Mimir for metrics, Tempo for traces.
_Avoid_: collector, database cluster

**Dashboard**: a saved set of telemetry queries and visualizations for an operator's question.
_Avoid_: console, landing page

**Alert**: a periodically evaluated condition over telemetry, with a visible state and an
optional delivery route.
_Avoid_: notification, alarm message

**Checkpoint**: one consistent backup set of configuration and persisted telemetry, taken
with ingestion paused, and the unit of restore and rollback before a persistent change.
_Avoid_: snapshot, dump

**Stack Gateway**: the Caddy instance that is the only published entry, serving the Stack
Console and routing to Grafana and the optional RustFS console.
_Avoid_: collector, mesh

**Stack Console**: the unauthenticated page at the root hostname with this stack's
application links, readiness, alert delivery state and the configured versions from the
Status Document; it links no sibling stacks.
_Avoid_: dashboard, admin UI

**Status Document**: the public Status v2 file bootstrap writes after readiness and the
Stack Gateway serves at `/status.json`, recording each component's configured image,
version, profile state and health path plus whether backups and alert delivery are
configured, never observed runtime state.
_Avoid_: status observation, versions file

**Platform Network**: the external Docker network `platform` shared by the stacks on one
host for ingress and collection, with the fixed allocation `172.30.0.0/24` and Platform
Edge at `172.30.0.2`.
_Avoid_: default network, public network

**Local Mode**: the default access mode, serving HTTP and internal-CA HTTPS on loopback
with HTTP as the browser scheme by default.
_Avoid_: development mode

**Public Mode**: the access mode for an operator's domain, with Let's Encrypt HTTPS and
HTTP redirects.
_Avoid_: production mode

**Proxy Mode**: the access mode behind Platform Edge or another gateway that handles HTTPS,
listening on HTTP only and trusting the configured proxies for the forwarded scheme.
_Avoid_: public mode, local mode

**Pinned Version**: the default image reference in `compose.yaml`, a stable tag plus an
immutable digest that has passed the Smoke Contract.
_Avoid_: latest, floating tag

**Smoke Contract**: the executable check (`scripts/smoke.sh`) that a disposable fresh
installation is healthy, authenticated and ingesting telemetry.
_Avoid_: unit suite, static validation
