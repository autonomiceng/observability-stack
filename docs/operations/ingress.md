# Ingress and access modes

Caddy is the only published entry.

| Hostname | Upstream |
| --- | --- |
| `<domain>` | Stack Console and exact `/health/*` probes |
| `OB_GRAFANA_HOST` (default `grafana.<domain>`) | Grafana |
| `OB_RUSTFS_HOST` (default `rustfs.<domain>`) | Opt-in native RustFS console and APIs, operator clients only |

`OB_GRAFANA_URL` optionally sets Grafana's full browser origin, including its port.
Leave it empty to keep the mode, hostname and port suffix defaults. It accepts an
HTTP or HTTPS origin with no trailing slash, path, credentials, query or fragment.
It sets Grafana's root URL and domain and the console links. `OB_GRAFANA_HOST` remains
the internal route hostname. The URL does not change listeners or issue certificates;
use proxy mode when Platform Edge serves it.

Set `OB_ACCESS_MODE` before running bootstrap. Bootstrap records the browser URL protocol, Grafana hostname and Compose file selection. Run bootstrap again after changing mode.

| Mode | Listeners | Certificates | HTTP behavior | Default external scheme |
| --- | --- | --- | --- | --- |
| Local (`local`, default) | HTTP and HTTPS | Self-signed | HTTP stays available | `http` |
| Public (`public`) | HTTP and HTTPS | Trusted certificates for your domain | Redirect to HTTPS, except health checks and `/status.json` | `https` |
| Behind another gateway (`proxy`) | HTTP from the gateway | The other gateway handles HTTPS | No redirect inside this stack | `https` |

`OB_SCHEME` is the browser URL protocol, independently of the listener protocol.
It may be `http` or `https` in local/proxy mode; public mode requires `https`. The mode selects certificates and listening ports automatically.

## Local Mode

Defaults are `OB_PUBLIC_DOMAIN=localhost`, `OB_SCHEME=http`, `OB_BIND_HOST=127.0.0.1`.
Both `http://localhost` and `https://localhost` work. The root also supports `127.0.0.1`.
Local HTTP accepts arbitrary root hostnames, but Grafana requires its configured hostname.
With an IP root, `OB_GRAFANA_HOST` defaults to `grafana.localhost`; set it explicitly for
another DNS name. Console links always use the configured external origin from `/links.json`.
No application origin is derived from the request's Host header.

Grafana is at `http://grafana.localhost` by default. Log in as `admin` using
`OB_GRAFANA_ADMIN_PASSWORD` from the private `.env` file.

On a host where the gateway owns ports 80/443, choose spare ports, for example `OB_HTTP_PORT=8080` and
`OB_HTTPS_PORT=8443` in `.env` before bootstrap. Bootstrap fills an empty
`OB_PUBLIC_PORT_SUFFIX` as `:8080`; the URL is then `http://grafana.localhost:8080`.
When changing ports later, update or clear that suffix too. If an external edge supplies the
public ports, set the suffix to the edge's public port, independently of the internal bind.
Bootstrap derives a suffix from the selected browser-facing protocol's published port in local
and public modes. Proxy mode never derives an external port from the internal HTTP port.

Caddy automatically issues and renews local certificates in the existing `caddy-data` volume.
Bootstrap verifies both protocols, using only the public CA certificate in memory for HTTPS.
It installs no host trust. Browser trust is an operator action: obtain this installation's
`/data/caddy/pki/authorities/local/root.crt` from Caddy and trust it on the required clients.
Never copy a CA private key or share certificate volumes between stacks. With Platform Edge,
choose `OB_ACCESS_MODE=proxy` so Edge handles HTTPS and this stack receives HTTP.

## Public Mode

Point DNS for the root and `grafana.<domain>` to the host, open ports 80/443 and set:

```sh
OB_PUBLIC_DOMAIN=observe.example.com
OB_GRAFANA_HOST=grafana.observe.example.com
OB_ACCESS_MODE=public
OB_SCHEME=
OB_BIND_HOST=0.0.0.0
```

Run bootstrap. Caddy obtains certificates and redirects HTTP to the configured HTTPS
origins. Exact root health routes remain available over HTTP without a redirect; backend
APIs remain private. Exact `/status.json` is also served over HTTP without a redirect and
exposes the [status document](maintenance.md#status-document). Public readiness verifies the certificate with system trust and sends
the configured hostname as TLS SNI while dialing the loopback published port.

## Behind another gateway

```sh
OB_ACCESS_MODE=proxy
OB_PUBLIC_DOMAIN=observe.example.com
OB_GRAFANA_HOST=grafana.observe.example.com
OB_SCHEME=https
OB_HTTP_PORT=18180
OB_TRUSTED_PROXIES=172.30.0.2/32
```

`172.30.0.2/32` is Edge's reserved Platform Network address and the default; see
[Shared host](#shared-host). Port 18180 is the contract's loopback HTTP port for this
stack behind Edge. Bootstrap selects
`compose.yaml:compose.proxy.yaml` (plus the S3 override when enabled); the override replaces
the port list, publishing only HTTP. Compose 2.24.4+ supports the required `!override` tag.
Do not bypass this selection with `-f compose.yaml` when operating a proxy installation.
Keep a loopback bind unless direct remote HTTP access is intentional. Edge forwards to
`ob-gateway:80`, retaining the configured root or Grafana Host and setting the external
`X-Forwarded-Proto`. Trusted forwarding preserves HTTPS for Grafana even though its
upstream connection is HTTP. No certificate or trust material is copied into this stack.

## Tailscale on one machine hostname

Platform Edge can serve Grafana on a separate HTTPS port of the machine's Tailscale
hostname. For example, set these values in this stack's existing `.env`:

```sh
OB_ACCESS_MODE=proxy
OB_PUBLIC_DOMAIN=host.tail-example.ts.net
OB_GRAFANA_HOST=grafana.localhost
OB_GRAFANA_URL=https://host.tail-example.ts.net:8447
OB_SCHEME=https
OB_PUBLIC_PORT_SUFFIX=:8446
OB_BIND_HOST=127.0.0.1
OB_HTTP_PORT=18180
OB_TRUSTED_PROXIES=172.30.0.2/32
```

Port 18180 is the stack's loopback HTTP port behind Edge. In this example port 8446 serves the Stack
Console and port 8447 serves Grafana. Edge owns the HTTPS listeners and certificates.
Keep the existing secrets, state directory, volume prefix and storage profile.
Run bootstrap again after changing the settings. It writes `OB_GRAFANA_URL_HOST`
and `OB_GRAFANA_AUTHORITY` to `data/derived.env` for Compose; these are derived values,
not operator settings.

Configure Edge's Grafana listener to forward to `ob-gateway:80`, preserve
`Host: host.tail-example.ts.net:8447`, and overwrite `X-Forwarded-Proto` with
`https`. Edge must also supply the connecting client's address correctly. The stack
accepts forwarded information only from its configured exact peers.

Caddy matches the full external authority, including `:8447`. Requests for the same
hostname on other ports stay on console routes. The internal `grafana.localhost`
route and root health routes continue to work. Keep the internal Grafana hostname
separate from the external machine hostname so internal routing has its own name.
Grafana login remains required, and `/metrics` remains blocked at the Stack Gateway.

The Stack Console reads this stack's application links from `/links.json` and configured
versions from `/status.json`. It links no sibling stacks; Platform Edge's console does.

## Optional RustFS human console

On an existing S3 installation, set `OB_RUSTFS_CONSOLE=true` and run bootstrap again.
The default is `false`, passed directly to RustFS's native `RUSTFS_CONSOLE_ENABLE`.
Bootstrap refuses enablement without the existing `s3` profile; it never selects S3
or migrates storage for the console. Keep existing secrets, volumes, state and Compose
profiles. A filesystem installation needs an explicit storage migration before this
feature can be used. The shipped RustFS pin and `OB_RUSTFS_IMAGE` override are unchanged.
Toggling the flag recreates RustFS and Caddy and can briefly fail S3 writes; schedule the
interruption and take a [Checkpoint](backup.md) before changing the setting.

Standalone ingress uses `rustfs.<domain>` (`rustfs.localhost` for an IP root), with an
optional `OB_RUSTFS_HOST` override. Its links follow the existing scheme and port suffix.
Local Mode supports HTTP and internal-CA HTTPS; Public Mode requires DNS for this extra
hostname and issues its certificate only when enabled. Disabling hides the console card and
returns 404 through HTTP/proxy ingress; standalone RustFS HTTPS is provisioned only while
enabled. No additional listener or published port is needed.

For private Tailscale access through Platform Edge, keep the existing Grafana and root
origins and configure:

```sh
OB_RUSTFS_CONSOLE=true
OB_RUSTFS_HOST=rustfs.localhost
OB_RUSTFS_URL=https://host.tail-example.ts.net:8451
```

Use the proxy settings above. `OB_RUSTFS_URL` follows the same strict origin rules as
`OB_GRAFANA_URL`; bootstrap writes `OB_RUSTFS_URL_HOST` and `OB_RUSTFS_AUTHORITY` to
`data/derived.env` as derived values. Grafana on `:8447`, the Stack Console on `:8446`, and RustFS on `:8451` can share
one hostname. Authorities must be distinct. Internal application hostnames stay separate
from the shared external hostname; bootstrap rejects cross-application hostname reuse,
even when the console is disabled. The URL configures routing and links; Edge owns the
external listener and certificate.

Platform Edge's follow-up reserves private HTTPS port 8451 and forwards its **whole
origin** to `ob-gateway:80`, retaining `Host: host.tail-example.ts.net:8451` and supplying
`X-Forwarded-Proto: https`. Edge must correctly overwrite or append the connecting client
address. Caddy forwards all paths unchanged to `rustfs:9001`, including
`/rustfs/console/`, its assets, `/rustfs/admin/v3/*`, STS and S3 requests. Rewriting Host,
dropping the port, stripping a prefix or routing only the UI breaks same-origin requests
and SigV4. Only GET/HEAD `/` requests accepting HTML redirect to `/rustfs/console/`.

The whole origin requires `OB_RUSTFS_CONSOLE_ALLOW`. Set it to the actual operator Tailnet
client IPs (including IPv6 when used), separately from `OB_TRUSTED_PROXIES`, which must
contain only Edge's exact connection peer IPs. If Tailscale termination presents its own
peer identity instead of an original client, verify that identity and enforce client
restrictions at that edge before allowing the peer. Never trust the whole Tailnet or a
Docker subnet as forwarding proxies. Untrusted forwarding headers cannot grant access.
RustFS still stays off the Platform Network; Caddy is its only ingress.

Humans log in using the existing RustFS root credentials: `OB_S3_ACCESS_KEY`
and `OB_S3_SECRET_KEY` in the private `.env`. These are administrative credentials, not an
agent's scoped S3 key. Native RustFS authentication remains required for admin and object
operations. The Stack Console publishes only the configured link. Agents use the Files
API in their owning stack; this console adds no agent credential distribution.

The S3 smoke fixture enables the console and checks HTML-only landing redirects, all
referenced JS/CSS assets, unsigned admin denial, forwarding spoof isolation and full
external port routing. The filesystem fixture checks disabled-origin 404s. Before operator
rollout, also verify native browser login and a signed `/rustfs/admin/v3/accountinfo`
request through Edge on port 8451, including trusted client allow/deny cases. The expected
signed response is 200; the unsigned response is 403 after the operator IP gate.

## Shared host

Caddy joins the external network `platform` as `ob-gateway`, Grafana as `ob-grafana`; the
gateway console can probe `http://ob-grafana:3000/api/health` directly. A shared edge can
reach `ob-gateway:80` while the stack uses spare loopback ports.

The Platform Network has one allocation on every host, defined in the shared contract
([conventions](../conventions.md)): subnet `172.30.0.0/24` (`OB_PLATFORM_SUBNET`), dynamic
range `172.30.0.128/25` (`OB_PLATFORM_IP_RANGE`) and gateway `172.30.0.1`. Platform Edge
holds the reserved address `172.30.0.2` outside the dynamic range. Whichever bootstrap runs
first creates the network with these parameters. Every bootstrap validates an existing
network and refuses a different subnet or range, or a network with no IPAM configuration,
with `platform_network_mismatch`. To repair a network created before this contract, stop
every stack on it, run `docker network rm` on the network the error names (`OB_PLATFORM_NETWORK`,
default `platform`), then rerun bootstrap. Every stack on the host must use the same values.

Loki, Mimir, Tempo, RustFS and Alloy have no published ports. Loki, Mimir, Tempo and Alloy
have no public Caddy API routes; RustFS has only the opt-in operator console origin above.
Grafana authenticates its datasource proxy. Caddy blocks Grafana's `/metrics` path. The
platform network is trusted: Grafana and Alloy's internal endpoints can be reached by its
members. Alloy joins as `ob-alloy`. Its OTLP receivers bind only to `ob-alloy-otlp` on
the project's default network, at ports 4317 (gRPC) and 4318 (HTTP). Opt-in producers must
join that network to send traces; the Platform Network cannot reach those listeners.

`OB_TRUSTED_PROXIES` defaults to Edge's reserved address, `172.30.0.2/32`, so no address
discovery is needed; an empty value uses the same default. Docker never assigns that address
dynamically, so without Edge the default grants nothing. Change it only for another gateway
or a different Platform Network subnet, and keep it to exact IPs (`/32` or `/128` also
accepted). Subnets and symbolic ranges are refused, and bootstrap refuses an
`OB_PLATFORM_IP_RANGE` that contains a trusted IPv4 proxy address. Bootstrap keeps an existing
nonempty value; replace an older discovered Edge IP with `172.30.0.2/32` once Edge holds its
reserved address.
Caddy accepts forwarded client IPs only from configured trusted proxies and parses the chain
from right to left. The edge must correctly overwrite or append the connecting client's IP.
Untrusted peers cannot gain access by supplying an `X-Forwarded-For` header. With no trusted
proxy, the client address remains the connection's direct peer.

Health routes have no client-address tier: `/health/<component>` returns the upstream status
code with an empty body to every caller, and `/health/alerts` returns `{"status":"degraded"}`
with 503 while alert delivery is unconfigured. Read details inside the stack, for example
`docker compose logs grafana` or `docker compose exec caddy wget -qO- http://grafana:3000/api/health`.
The only client-address gate is the native RustFS console's `OB_RUSTFS_CONSOLE_ALLOW`.
