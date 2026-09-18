# Change map

For Compose or storage changes, read the storage ADRs and operations runbooks.
Validate both filesystem and S3 configurations. A storage-mode change is a
migration; preserve external volumes and service names.

For Alloy or Grafana provisioning changes, verify exporter aliases, authentication,
datasources and alert provisioning through the existing smoke contract. Distroless
backends are covered by aggregate health probes rather than individual Docker
health fields.

For bootstrap or environment changes, preserve present secrets and unmanaged lines.
Unit tests use a fake runner and never call Docker. Run the restore drill separately with `SMOKE_PROFILE=filesystem` and `SMOKE_PROFILE=s3`.
Both restore into empty disposable storage and verify historical telemetry and Grafana state;
S3 also verifies persisted Loki/Mimir object bodies. Until each profile passes, report its
recovery as unverified. See `docs/operations/backup.md` for the remaining coverage limits.

Run the gates in `CONTRIBUTING.md`. `scripts/validate.sh` starts disposable image
validators; it does not start the installed stack. Image, config and bootstrap
changes also need `scripts/smoke.sh`. Report untested storage profiles explicitly.
