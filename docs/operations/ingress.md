# Ingress and access modes

How the stack is reached: hostnames, the three access modes, Platform Edge, Tailscale, the
optional RustFS console, and what the shared network exposes.

- [Hostnames and modes](#hostnames-and-modes)
- [Local Mode (default)](#local-mode-default)
- [Public Mode](#public-mode)
- [Behind Platform Edge](#behind-platform-edge)
- [Tailscale](#tailscale)
- [Application URLs](#application-urls)
- [RustFS admin console](#rustfs-admin-console)
- [Shared host](#shared-host)
- [Health routes](#health-routes)
- [Troubleshooting](#troubleshooting)

## Hostnames and modes

Caddy is the only published entry.

| Hostname | Upstream |
| --- | --- |
| `<domain>` (`OB_PUBLIC_DOMAIN`) | Stack Console and exact `/health/*` probes |
| `OB_GRAFANA_HOST` (default `grafana.<domain>`) | Grafana |
| `OB_RUSTFS_HOST` (default `rustfs.<domain>`) | Opt-in native RustFS console and APIs, operator clients only |

`OB_ACCESS_MODE` selects the listeners and the default browser scheme. Set it before
running bootstrap; bootstrap records the Compose file selection and writes the derived
values to `data/derived.env`. Run bootstrap again after changing the mode.

| Mode | Listeners | Certificates | HTTP behavior | Default `OB_SCHEME` |
| --- | --- | --- | --- | --- |
| Local (`local`, default) | HTTP and HTTPS | Self-signed | HTTP stays available | `http` |
| Public (`public`) | HTTP and HTTPS | Let's Encrypt for your domain | Redirect to HTTPS, except health checks and `/status.json` | `https` |
| Proxy (`proxy`) | HTTP from the gateway | The other gateway handles HTTPS | No redirect inside this stack | `https` |

`OB_SCHEME` is the browser URL protocol, independently of the listener protocol. It may
be `http` or `https` in Local and Proxy Mode; Public Mode requires `https`. This stack's
Caddy has no certificate issuer setting: for a private ACME CA or certificate files, put
the stack behind Platform Edge, which has `PE_TLS_ISSUER`.

## Local Mode (default)

Defaults are `OB_PUBLIC_DOMAIN=localhost`, an empty `OB_SCHEME` (HTTP links) and
`OB_BIND_HOST=127.0.0.1`. Both `http://localhost` and `https://localhost` work; the root
also answers on `127.0.0.1`. Local HTTP accepts arbitrary root hostnames, but Grafana
requires its configured hostname: with an IP root, `OB_GRAFANA_HOST` defaults to
`grafana.localhost`; set it explicitly for another DNS name. Console links always use the
configured external origin from `/links.json`; no origin is derived from the request Host.

Grafana is at `http://grafana.localhost`. Log in as `admin` with
`OB_GRAFANA_ADMIN_PASSWORD` from the private `.env`.

If another stack owns ports 80 and 443 on the host, choose spare ports before bootstrap,
for example `OB_HTTP_PORT=8080` and `OB_HTTPS_PORT=8443`. Bootstrap fills an empty
`OB_PUBLIC_PORT_SUFFIX` as `:8080` (from the published port of the browser-facing
protocol), so the URL becomes `http://grafana.localhost:8080`. When changing ports later,
update or clear that suffix too. Proxy Mode never derives a suffix from the internal port.

Caddy issues and renews local certificates from its own CA in the existing `caddy-data`
volume. Bootstrap verifies both protocols with the public CA certificate in memory and
installs no host trust. To remove browser warnings, export only the public root and trust
it on the clients that need it:

```sh
docker compose cp caddy:/data/caddy/pki/authorities/local/root.crt ./observability-root.crt
```

Never copy a CA private key or share certificate volumes between stacks.

## Public Mode

Point DNS for the root and `grafana.<domain>` to the host, open ports 80 and 443, and set:

```sh
OB_PUBLIC_DOMAIN=observe.example.com
OB_GRAFANA_HOST=grafana.observe.example.com
OB_ACCESS_MODE=public
OB_SCHEME=
OB_BIND_HOST=0.0.0.0
```

Run bootstrap. Caddy obtains certificates and redirects HTTP to the configured HTTPS
origins. Exact root health routes and `/status.json` stay available over HTTP without a
redirect ([status document](maintenance.md#status-document)); backend APIs stay private.
Public readiness verifies the certificate with system trust and sends the configured
hostname as SNI while dialing the loopback published port. With the RustFS console enabled,
DNS for `rustfs.<domain>` is needed too.

## Behind Platform Edge

Edge owns ports 80 and 443 on a shared host, terminates HTTPS and forwards to
`ob-gateway:80`. Its bundle installer (`python3 scripts/bootstrap.py --with observability`
in the Edge checkout) writes the settings below into this stack's `.env` and runs this
bootstrap; to do it by hand, set:

```sh
OB_ACCESS_MODE=proxy
OB_PUBLIC_DOMAIN=observe.example.com
OB_SCHEME=https
OB_BIND_HOST=127.0.0.1
OB_HTTP_PORT=18180
OB_PUBLIC_PORT_SUFFIX=
OB_TRUSTED_PROXIES=172.30.0.2/32
```

Port 18180 is the contract's loopback HTTP port for this stack behind Edge, and
`172.30.0.2/32` is Edge's fixed Platform Network address and the default (see
[shared host](#shared-host)). Bootstrap selects `compose.yaml:compose.proxy.yaml` (plus
the S3 override when enabled); the override replaces the port list and publishes only
HTTP. Compose 2.24.4 or newer is required for its `!override` tag. Do not bypass this
selection with `-f compose.yaml` on a proxy installation. Keep a loopback bind unless
direct remote HTTP access is intentional. Edge keeps the configured root or Grafana Host
and sets `X-Forwarded-Proto`; because Edge is a trusted proxy, Grafana keeps its HTTPS
origin although its upstream connection is HTTP. No certificate or trust material is
copied into this stack.

## Tailscale

Private access from your own devices without exposing anything to the internet.

**Behind Platform Edge** (shared host): Edge runs one Tailscale node per hostname and
gives Grafana the origin `https://grafana.<tailnet>.ts.net`. Its
`python3 scripts/bootstrap.py --tailscale --with observability` writes that origin into
`OB_GRAFANA_URL` and reruns this bootstrap; nothing else changes here. See Edge's
[Tailscale setup](https://github.com/autonomiceng/platform-edge/blob/main/docs/operations/tailscale.md).

**Standalone**: the host's own Tailscale daemon serves the console and Grafana on two
HTTPS ports of the machine's tailnet name and forwards to the stack's loopback HTTP port.
Prerequisites: Tailscale installed and logged in on the host, MagicDNS and HTTPS
certificates enabled for the tailnet (`https://login.tailscale.com/admin/dns`).

1. In `.env`, select Proxy Mode on a loopback port and give Grafana its full browser
   origin; the console uses the domain plus the port suffix:

   ```sh
   OB_ACCESS_MODE=proxy
   OB_PUBLIC_DOMAIN=host.tail-example.ts.net
   OB_GRAFANA_HOST=grafana.localhost
   OB_GRAFANA_URL=https://host.tail-example.ts.net:8447
   OB_SCHEME=https
   OB_PUBLIC_PORT_SUFFIX=:8446
   OB_BIND_HOST=127.0.0.1
   OB_HTTP_PORT=18180
   ```

   Keep the internal Grafana hostname separate from the external machine hostname so the
   internal route keeps its own name. Bootstrap writes `OB_GRAFANA_URL_HOST` and
   `OB_GRAFANA_AUTHORITY` to `data/derived.env`; they are derived values, not settings.
2. Run `python3 scripts/bootstrap.py`, then find the address the host daemon connects
   from. Connections to the published loopback port reach Caddy from the project
   network's gateway, not from Edge's address:

   ```sh
   docker network inspect observability-stack_default --format '{{(index .IPAM.Config 0).Gateway}}'
   ```

   Set `OB_TRUSTED_PROXIES` to that address as a `/32` and run bootstrap again. Caddy
   accepts a forwarded scheme only from trusted proxies; from an untrusted peer Grafana
   receives `X-Forwarded-Proto: http`, and its HTTPS login cookies and redirects break.
   Trusting the bridge gateway means every host-local connection to the loopback port can
   assert the scheme; the port is bound to loopback, so only processes on this host can.
3. Serve both ports from the host daemon:

   ```sh
   tailscale serve --bg --https=8446 http://127.0.0.1:18180
   tailscale serve --bg --https=8447 http://127.0.0.1:18180
   ```

   `tailscale serve status` lists the result; `tailscale serve --https=<port> --set-path=/ off`
   removes one entry.

Verify from a tailnet device: open `https://host.tail-example.ts.net:8446/`, follow the
Grafana link and sign in. A login that loops back to the sign-in page means the scheme is
not trusted (step 2). Caddy matches Grafana by its full authority including `:8447`, so the
forwarding proxy must preserve `Host` (Tailscale serve does); requests for the same
hostname on other ports stay on console routes. Grafana login remains required, and
`/metrics` stays blocked at the gateway. Who can reach these ports is decided by your
tailnet access controls.

## Application URLs

`OB_GRAFANA_URL` sets Grafana's full browser origin, including its port; empty keeps the
mode, hostname and port suffix defaults. It accepts an HTTP or HTTPS origin with no
trailing slash, path, credentials, query or fragment, and sets Grafana's root URL and
domain and the console link. `OB_GRAFANA_HOST` remains the internal route hostname. The
URL changes no listener and issues no certificate. `OB_RUSTFS_URL` does the same for the
RustFS console. Authorities must be distinct, and bootstrap rejects a URL that reuses
another application's hostname (`rustfs_origin_conflict`). Run bootstrap after changing
either.

The Stack Console reads this stack's links from `/links.json` and configured versions from
`/status.json`. It links no sibling stacks; Platform Edge's console does.

## RustFS admin console

On an existing S3 installation, set `OB_RUSTFS_CONSOLE=true` and run bootstrap again. The
default is `false`, passed directly to RustFS's `RUSTFS_CONSOLE_ENABLE`. Bootstrap refuses
enablement without the existing `s3` profile (`rustfs_console_requires_s3`); it never
selects S3 or migrates storage for the console. Toggling the flag recreates RustFS and
Caddy and can briefly fail S3 writes; schedule the interruption and take a
[Checkpoint](backup.md) first.

Standalone ingress uses `rustfs.<domain>` (`rustfs.localhost` for an IP root), with an
optional `OB_RUSTFS_HOST` override; links follow the scheme and port suffix. Local Mode
serves HTTP and internal-CA HTTPS; Public Mode needs DNS for the extra hostname and issues
its certificate only while enabled. Disabled, the console card is hidden, the HTTP and proxy
routes answer 404, and the standalone HTTPS site for the console hostname is not
provisioned at all. Caddy forwards the whole origin to private port 9001, including the console
assets, `/rustfs/admin/v3/*`, STS and S3 requests, preserving Host and port; only GET and
HEAD `/` requests accepting HTML redirect to `/rustfs/console/`. Rewriting Host, dropping
the port or routing only the UI breaks same-origin requests and SigV4.

`OB_RUSTFS_CONSOLE_ALLOW` (default `127.0.0.1/8 ::1`) lists the client addresses allowed
to reach the origin; other clients receive 404. Keep it separate from `OB_TRUSTED_PROXIES`
and never allow a whole tailnet or Docker subnet. Behind Platform Edge the console is not a
Tailnet Origin; reach it for a session over the host. A connection to the published
loopback port reaches Caddy from the project network's gateway, not from `127.0.0.1`, so
allow that one address for the session:

```sh
docker network inspect observability-stack_default --format '{{(index .IPAM.Config 0).Gateway}}'
```

Add it as a `/32` to `OB_RUSTFS_CONSOLE_ALLOW` (for example
`OB_RUSTFS_CONSOLE_ALLOW="127.0.0.1/8 ::1 172.19.0.1/32"`), run bootstrap, then from your
machine `ssh -L 18180:127.0.0.1:18180 <host>`, add a hosts entry mapping `rustfs.<domain>`
to `127.0.0.1`, and open `http://rustfs.<domain>:18180/rustfs/console/`; Caddy routes the
console by that hostname, so an IP URL does not reach it. Sign in, and afterwards remove
the address again and rerun bootstrap. RustFS stays off the Platform Network; Caddy is its
only ingress.

Humans log in with the RustFS root credentials `OB_S3_ACCESS_KEY` and `OB_S3_SECRET_KEY`
from the private `.env`. These are administrative credentials, not an agent's scoped S3
key; agents use the Files API of their own stack.

## Shared host

Caddy joins the external network `platform` as `ob-gateway`, Grafana as `ob-grafana` and
Alloy as `ob-alloy`. Loki, Mimir, Tempo, RustFS and Alloy publish no ports. Loki, Mimir,
Tempo and Alloy have no public Caddy routes; RustFS has only the opt-in console origin.
Grafana authenticates its datasource proxy, and Caddy blocks Grafana's `/metrics`. The
Platform Network is trusted: its members can reach Grafana's and Alloy's internal
endpoints. Alloy's OTLP receivers bind only to `ob-alloy-otlp` on the project's default
network (ports 4317 gRPC and 4318 HTTP); producers join that network to send traces.

The Platform Network has one allocation on every host, defined in the shared contract
([conventions](../conventions.md)): subnet `172.30.0.0/24` (`OB_PLATFORM_SUBNET`), dynamic
range `172.30.0.128/25` (`OB_PLATFORM_IP_RANGE`) and gateway `172.30.0.1`. Platform Edge
holds the reserved address `172.30.0.2` outside the dynamic range. Whichever bootstrap runs
first creates the network with these parameters. Every bootstrap validates an existing
network and refuses a different subnet or range, or a network with no IPAM configuration,
with `platform_network_mismatch`.

`OB_TRUSTED_PROXIES` defaults to Edge's reserved address, `172.30.0.2/32`; an empty value
uses the same default. Docker never assigns that address dynamically, so without Edge the
default grants nothing. Change it only for another gateway or a different subnet, and keep
it to exact addresses (`/32` or `/128`); subnets and symbolic ranges are refused
(`proxy_trust_invalid`), and bootstrap refuses an `OB_PLATFORM_IP_RANGE` that contains a
trusted IPv4 proxy address. Caddy accepts forwarded client addresses and schemes only from
these peers, parsing the chain from right to left; an untrusted peer cannot gain anything
with `X-Forwarded-For`.

## Health routes

`/health/<component>` returns the upstream status code with an empty body to every
caller; there is no client-address tier. `/health/alerts` returns `{"status":"degraded"}`
with 503 while alert delivery is unconfigured and `{"status":"ready"}` otherwise. Read
details inside the stack, for example `docker compose logs grafana` or
`docker compose exec caddy wget -qO- http://grafana:3000/api/health`. The component list is
in [status document](maintenance.md#status-document).

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `access_mode_invalid`, `access_host_invalid`, `access_port_invalid` | `OB_ACCESS_MODE`, a hostname or a port setting is malformed; the message names it. |
| `compose_file_conflict` | A shell `COMPOSE_FILE` disagrees with the recorded selection, a selected file is missing, or the list lacks the overlays for the selected storage and access modes. Unset the shell value or edit `.env`. |
| `platform_network_mismatch` | The shared network exists with another subnet or range. Stop every stack on it, `docker network rm` the network the error names (`OB_PLATFORM_NETWORK`, default `platform`), rerun bootstrap. |
| `proxy_trust_invalid` | `OB_TRUSTED_PROXIES` holds a subnet or range. Use exact addresses. |
| `rustfs_console_requires_s3` | The console needs the `s3` profile; a filesystem installation cannot enable it without a storage migration. |
| `storage_migration_required` | `COMPOSE_PROFILES` changed the storage mode of an existing installation. Restore the previous mode; migrating data is a separate procedure. |
| Grafana login loops behind a proxy | The proxy is not in `OB_TRUSTED_PROXIES`, so Grafana sees `X-Forwarded-Proto: http`. Trust the exact peer. |
| Grafana answers 404 through a proxy | The request `Host` (including port) matches neither `OB_GRAFANA_HOST` nor `OB_GRAFANA_URL`. Compare `/links.json` with the URL the browser used. |
| Bootstrap ends `degraded` with `alert_delivery_placeholder` | Expected until alert delivery is configured; see [alerts](maintenance.md#alerts-and-metric-contracts). |
