# Observability Stack

The shared operational view of the stacks on one host.

## Language

**Collector**:
The component that discovers producers and routes their logs, metrics and traces to a Backend.
_Avoid_: Agent, shipper

**Backend**:
A durable store and query interface for one telemetry signal: logs, metrics or traces.
_Avoid_: Collector, database cluster

**Dashboard**:
A saved set of telemetry queries and visualizations for an operator's question.
_Avoid_: Console, landing page

**Alert**:
A periodically evaluated condition over telemetry, with a visible state and an optional delivery route.
_Avoid_: Notification, alarm message

**Checkpoint**:
One consistent backup set of configuration, secrets and persisted telemetry, taken with ingestion paused.
It is the unit of restore and the rollback boundary before a persistent change.
_Avoid_: Snapshot, dump

**Stack Gateway**:
The single published entry that serves the Stack Console and routes application requests.
_Avoid_: Collector, mesh

**Stack Console**:
The unauthenticated page with this stack's application links, readiness, alert delivery state
and the configured versions from the Status Document. It links no sibling stacks.
_Avoid_: Dashboard, admin UI

**Status Document**:
The public Status v2 file bootstrap writes after readiness and the Stack Gateway serves at
`/status.json` in every access mode: each component's configured image and version, whether
the selected profiles enable it, its health path, application origins, whether backup and
alert delivery are configured, and the newest Checkpoint time at that bootstrap. It records
configuration, never observed runtime state.
_Avoid_: Status observation, versions file

**Platform Network**:
The trusted Docker network shared by sibling stacks on one host for ingress and collection, with the
fixed allocation `172.30.0.0/24` and Platform Edge at the reserved address `172.30.0.2`.
_Avoid_: Default network, public network

**Local Mode**:
Local access over both HTTP and privately issued HTTPS, with HTTP as the canonical origin by default.
_Avoid_: Development mode

**Public Mode**:
Publicly certified HTTPS access through the Stack Gateway on an operator's domain, with HTTP redirects.
_Avoid_: Production mode

**Proxy Mode**:
HTTP access behind Platform Edge, which owns the external TLS connection and forwards the configured public origin.
_Avoid_: Public Mode, Local Mode

**Pinned Version**:
An image identified by a stable tag and immutable digest that has passed the Smoke Contract.
_Avoid_: Latest, floating tag

**Effective Image**:
The image selected by native Compose after applying an optional `OB_*_IMAGE` override.
A tag or local image is an operator experiment; the shipped Pinned Version remains the default.

**Smoke Contract**:
The executable proof that a disposable fresh installation is healthy, authenticated and ingesting telemetry.
_Avoid_: Unit suite, static validation
