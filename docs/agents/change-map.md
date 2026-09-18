# Change map

For Compose or storage changes, read the storage ADRs and operations runbooks.
Validate both filesystem and S3 configurations. A storage-mode change is a
migration; preserve external volumes and service names.

For Alloy or Grafana provisioning changes, verify exporter aliases, authentication,
datasources and alert provisioning through the existing smoke contract. Distroless
backends are covered by aggregate health probes rather than individual Docker
health fields.

For bootstrap or environment changes, preserve present secrets and unmanaged lines.
Unit tests use a fake runner and never call Docker. The existing restore drill
validates filesystem storage only. Backup/restore changes also affecting S3 need
a separate disposable S3 restore exercise; until that passes, report S3 recovery
as unverified.

Run the gates in `CONTRIBUTING.md`. `scripts/validate.sh` starts disposable image
validators; it does not start the installed stack. Image, config and bootstrap
changes also need `scripts/smoke.sh`. Report untested storage profiles explicitly.
