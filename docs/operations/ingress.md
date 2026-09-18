# Ingress and access modes

Caddy is the only published entry.

| Hostname | Upstream |
| --- | --- |
| `<domain>` | Stack Console and exact `/health/*` probes |
| `grafana.<domain>` | Grafana |

## Local Mode

Defaults are `OB_PUBLIC_DOMAIN=localhost`, `OB_SCHEME=http`, `OB_BIND_HOST=127.0.0.1`.
Grafana is at `http://grafana.localhost`. Log in as `admin` using
`OB_GRAFANA_ADMIN_PASSWORD` from the private `.env` file.

On a host where the gateway owns ports 80/443, choose spare ports, for example `OB_HTTP_PORT=8080` and
`OB_HTTPS_PORT=8443` in `.env` before bootstrap. Bootstrap fills an empty
`OB_PUBLIC_PORT_SUFFIX` as `:8080`; the URL is then `http://grafana.localhost:8080`.
When changing ports later, update or clear that suffix too. If an external edge supplies the
public ports, set the suffix to the edge's public port, independently of the internal bind.
Behind an edge serving standard HTTPS, set `OB_SCHEME=https`, `OB_LISTEN_SCHEME=http`
and leave `OB_PUBLIC_PORT_SUFFIX` empty. Bootstrap derives a suffix only when public and
listener schemes match and the selected port is non-default. Readiness always probes
`http(s)://127.0.0.1:<listen port>/health/<service>` with `Host: OB_PUBLIC_DOMAIN`.
HTTPS sends that public name as TLS SNI and verifies its certificate.

## Public Mode

Point DNS for the root and `grafana.<domain>` to the host, open ports 80/443 and set:

```sh
OB_PUBLIC_DOMAIN=observe.example.com
OB_SCHEME=https
OB_TLS_ISSUER=acme
OB_BIND_HOST=0.0.0.0
```

Run bootstrap. Caddy obtains certificates and redirects HTTP to HTTPS. With
`OB_TLS_ISSUER=internal`, install Caddy's root certificate on clients and on the bootstrap
host before readiness checks can succeed. The certificate lives at
`/data/caddy/pki/authorities/local/root.crt` in the Caddy container.

## Shared host

Bootstrap creates the external network `platform`. Caddy is `ob-gateway`, Grafana is
`ob-grafana`; the gateway console can probe `http://ob-grafana:3000/api/health` directly.
A shared edge can reach `ob-gateway:80` while the stack uses spare loopback ports.

Set `OB_GATEWAY_URL` and `OB_BACKPLANE_URL` to the sibling consoles' actual browser URLs.
The optional cards probe over `platform`; a never-seen missing stack displays "not installed".
Set `OB_GATEWAY_HEALTH_HOST` to the root hostname configured on the sibling gateway's Caddy;
it defaults to `localhost`. Its internal HTTP health endpoint must be reachable at
`lg-gateway:80/health/litellm`. An HTTPS-only sibling ingress requires matching internal
routing at the shared edge; this probe does not bypass TLS or guess its configuration.

Loki, Mimir, Tempo, RustFS and Alloy have no published ports or public Caddy API routes.
Grafana authenticates its datasource proxy. Caddy blocks Grafana's `/metrics` path. The
platform network is trusted: Grafana and Alloy's internal endpoints can be reached by its
members. Alloy joins as `ob-alloy`. Its OTLP receivers bind only to `ob-alloy-otlp` on
the project's default network, at ports 4317 (gRPC) and 4318 (HTTP). Opt-in producers must
join that network to send traces; the Platform Network cannot reach those listeners.

`OB_TRUSTED_PROXIES` is empty for standalone ingress. Behind platform-edge, set it to
only the edge's platform subnet. Detailed `/versions.json` and upstream health bodies
require the direct peer to match `OB_OPERATOR_ALLOW`, which defaults to loopback.
Other callers receive status-only responses. Docker port forwarding may present the bridge
address as the peer even for host loopback requests; add only a verified operator source
if detailed local responses are needed. Allowing an edge address grants details to all
requests forwarded by that edge, so the edge must enforce its own operator restrictions.
