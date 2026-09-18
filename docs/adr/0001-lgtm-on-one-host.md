# LGTM on one host, with Mimir and filesystem storage

Accepted. Owner decision, 2026-09-17.

Use Grafana, Loki, Tempo and monolithic Mimir on one host, with Alloy as the only Collector
and Caddy as the only published entry. Mimir supplies the remote-write and PromQL Backend
with a path to object storage and later distribution; this costs more operational complexity
than a local Prometheus server. Filesystem storage is the default to keep a fresh install
independent of an object store; the optional S3 profile uses local RustFS and requires an
explicit data migration when switching an existing installation.

This deployment has one failure domain and no high availability. Local retention is 30 days
for logs and metrics, seven days for traces. Datastores remain on the private project network;
only ingress and collection endpoints join the trusted Platform Network. Existing domain
traces remain in their owning stack; Tempo accepts opt-in OTLP producers.
