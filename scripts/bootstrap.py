#!/usr/bin/env python3
"""Bring the Observability Stack up from a clean checkout, or refuse with a reason.

Lock the env file, fill in
missing secrets, refuse to invent secrets over existing data, create or validate the shared
platform network allocation, start the stack, wait for readiness, print the next step. Exit codes: 0 ready,
1 refused (the JSON line on stderr names why), 2 bad usage, 3 the stack did not become
ready. Python 3.11+ standard library only.
"""

from __future__ import annotations

import argparse
import fcntl
import hmac
import json
import http.client
import ipaddress
import socket
import ssl
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Callable

PROJECT = "observability-stack"
NETWORK = "platform"
# Platform Network allocation shared by every stack (docs/conventions.md). Edge's reserved
# address lies outside the dynamic range, so siblings can trust it without discovery.
PLATFORM_SUBNET = "172.30.0.0/24"
PLATFORM_IP_RANGE = "172.30.0.128/25"
EDGE_PROXY = "172.30.0.2/32"
VOLUMES = ("caddy-data", "caddy-config", "grafana-data", "alloy-data",
           "loki-data", "tempo-data", "mimir-data", "rustfs-data")
TLS_OVERLAYS = ("compose.files.yaml", "compose.acme-ca-root.yaml", "compose.acme-eab.yaml")
SAN_NAME = re.compile(r"DNS:([^,\s]+)")

# Secrets the stack needs and how many random bytes each gets (hex encoded).
SECRETS: dict[str, int] = {
    "OB_GRAFANA_ADMIN_PASSWORD": 24,
    "OB_S3_ACCESS_KEY": 10,
    "OB_S3_SECRET_KEY": 24,
}
MANAGED = set(SECRETS)
# Bootstrap's resolved view of the env file, written to data/derived.env for Compose and
# Checkpoint. The operator's env file records only explicit shell choices and COMPOSE_FILE.
SAVED = (
    "COMPOSE_PROJECT_NAME", "OB_STATE_DIR", "OB_VOLUME_PREFIX",
    "OB_ACCESS_MODE", "OB_PUBLIC_DOMAIN", "OB_GRAFANA_HOST", "OB_SCHEME",
    "OB_BIND_HOST", "OB_HTTP_PORT", "OB_HTTPS_PORT", "OB_PUBLIC_PORT_SUFFIX",
    "OB_TRUSTED_PROXIES", "OB_GRAFANA_URL", "OB_GRAFANA_URL_HOST", "OB_GRAFANA_AUTHORITY",
    "OB_RUSTFS_CONSOLE", "OB_RUSTFS_CONSOLE_ALLOW", "OB_RUSTFS_HOST", "OB_RUSTFS_URL",
    "OB_RUSTFS_URL_HOST", "OB_RUSTFS_AUTHORITY", "OB_PLATFORM_SUBNET", "OB_PLATFORM_IP_RANGE",
    "OB_PLATFORM_URL", "OB_TLS_ISSUER", "OB_ACME_EMAIL", "OB_ACME_CA", "OB_ACME_CA_ROOT", "OB_ACME_EAB_KEY_ID",
    "OB_ACME_EAB_HMAC", "OB_TLS_DIR", "OB_TLS_CA",
)
# Earlier bootstraps saved these in the env file; they are always recomputed.
DERIVED_ONLY = ("OB_GRAFANA_URL_HOST", "OB_GRAFANA_AUTHORITY", "OB_RUSTFS_URL_HOST", "OB_RUSTFS_AUTHORITY")
IMAGE_LINE = re.compile(r"^\s+image:\s+(?P<ref>\S+)\s*$")
ENV_LINE = re.compile(r"^(?:export\s+)?(?P<key>[A-Z][A-Z0-9_]*)=(?P<value>.*)$")
# Status v2 components (docs/conventions.md): the contract's stable ID, which is also the
# Compose service, then display name and kind.
COMPONENTS = (
    ("caddy", "Caddy", "gateway"),
    ("grafana", "Grafana", "app"),
    ("alloy", "Alloy", "collector"),
    ("loki", "Loki", "datastore"),
    ("mimir", "Mimir", "datastore"),
    ("tempo", "Tempo", "datastore"),
    ("rustfs", "RustFS", "datastore"),
)
# Every component ships dotted numeric release tags, some with a `v` or a pre-release suffix.
RELEASE_TAG = r"v?[0-9]+(?:\.[0-9]+)+(?:-[A-Za-z0-9.]+)?"


class Refused(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


Runner = Callable[..., subprocess.CompletedProcess[str]]


def run(argv: list[str], *, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, text=True, capture_output=True, check=False, timeout=timeout)


def read_env(path: Path) -> tuple[list[str], dict[str, str]]:
    """Return the raw lines and the managed assignments. Unmanaged lines are kept verbatim."""
    if not path.exists():
        return [], {}
    lines = path.read_text(encoding="utf-8").splitlines()
    values: dict[str, str] = {}
    for line in lines:
        match = ENV_LINE.match(line)
        if not match:
            continue
        key, value = match.group("key"), unquote(match.group("value"))
        if key in MANAGED:
            if key in values:
                raise Refused("env_repair_required", f"{key} is set twice in {path}")
            # Bootstrap does not interpolate; a secret must be the literal Compose also reads.
            if not value or "$" in value:
                raise Refused("env_repair_required", f"{key} must be a literal value in {path}")
            values[key] = value
    return lines, values


def generate(missing: set[str]) -> dict[str, str]:
    fresh: dict[str, str] = {}
    for key in sorted(missing):
        fresh[key] = secrets.token_hex(SECRETS[key])
    return fresh


def write_env(path: Path, lines: list[str]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def unquote(value: str) -> str:
    """Compose accepts 'x' and "x"; a bare value is used as is."""
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def project_name(settings: dict[str, str]) -> str:
    """Same precedence as Compose: shell environment, then the env file, then name:."""
    return os.environ.get("COMPOSE_PROJECT_NAME") or unquote(settings.get("COMPOSE_PROJECT_NAME", "")) or PROJECT


def installation_state(root: Path, data_dir: Path, runner: Runner, project: str = PROJECT, prefix: str | None = None) -> list[str]:
    """Every place an earlier installation of this project could have left data."""
    found: list[str] = []
    prefixes = (f"{project}_", f"{prefix or project}_")
    if data_dir.exists():
        if not os.access(data_dir, os.R_OK | os.X_OK):
            raise Refused("data_dir_unreadable", str(data_dir))
        if any(data_dir.iterdir()):
            found.append(f"data at {data_dir}")
    result = runner(["docker", "volume", "ls", "--format", "{{.Name}}"])
    if result.returncode != 0:
        raise Refused("docker_unavailable", result.stderr.strip())
    names = {name for name in result.stdout.split() if name.startswith(prefixes)}
    labelled = runner(["docker", "volume", "ls", "--filter",
                       f"label=com.docker.compose.project={project}", "--format", "{{.Name}}"])
    if labelled.returncode != 0:
        raise Refused("docker_unavailable", labelled.stderr.strip())
    for name in sorted(names | set(labelled.stdout.split())):
        found.append(f"volume {name}")
    return found


def images(compose: Path) -> dict[str, str]:
    """Shipped service references only; ignores env files and the shell."""
    out: dict[str, str] = {}
    service = ""
    for line in compose.read_text(encoding="utf-8").splitlines():
        head = re.match(r"^  (?P<name>[a-z][a-z0-9-]*):\s*$", line)
        if head:
            service = head.group("name")
        match = IMAGE_LINE.match(line)
        if match and service:
            ref = match.group("ref")
            fallback = re.fullmatch(r"\$\{OB_[A-Z0-9_]+_IMAGE:-(.+)\}", ref)
            out[service] = fallback[1] if fallback else ref
            if '${' in out[service]:
                raise Refused('image_default_unrecognized', service)
    return out


def console_dir(state: Path) -> Path:
    """Caddy mounts this directory; Docker would create a missing one owned by root."""
    console = state / "console"
    console.mkdir(mode=0o755, parents=True, exist_ok=True)
    # Caddy runs without CAP_DAC_OVERRIDE; restore runs under umask 077.
    os.chmod(console, 0o755)
    return console


def console_links(settings: dict[str, str]) -> dict[str, str]:
    links = {"grafana": grafana_origin(settings)}
    if settings.get("OB_RUSTFS_CONSOLE") == "true":
        links["rustfs"] = rustfs_origin(settings)
    # The Edge console, linked as the platform home; a standalone stack has none.
    if settings.get("OB_PLATFORM_URL"):
        links["platform"] = settings["OB_PLATFORM_URL"]
    return links


def write_links(state: Path, settings: dict[str, str]) -> None:
    write_file(console_dir(state) / "links.json", json.dumps(console_links(settings)) + "\n", 0o644)


def utc(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def last_checkpoint(backups: Path) -> str | None:
    """Newest readable Checkpoint manifest time, or None when none can be read."""
    times = []
    try:
        paths = [path for path in backups.iterdir() if re.fullmatch(r"[0-9]{8}T[0-9]{12}Z", path.name)]
    except OSError:
        return None
    for path in paths:
        try:
            moment = datetime.fromisoformat(json.loads((path / "manifest.json").read_text())["timestamp"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        # Checkpoints record UTC offsets; a naive time cannot be ordered against them.
        if moment.tzinfo:
            times.append(moment)
    return utc(max(times)) if times else None


def status_document(available: dict, selected: dict, settings: dict[str, str], backups: Path,
                    configured_at: str) -> dict:
    """The public Status v2 document: configured images and origins, never secrets."""
    links = console_links(settings)
    components = []
    for component, name, kind in COMPONENTS:
        if component not in available:
            continue
        image = available[component]["image"].split("@", 1)[0]
        tag = image.rsplit(":", 1)[1] if ":" in image.rsplit("/", 1)[-1] else ""
        record = {"id": component, "name": name, "kind": kind, "enabled": component in selected,
                  "image": image,
                  "version": tag if re.fullmatch(RELEASE_TAG, tag) else None,
                  "health": "/health/" + component}
        if component in links:
            record["url"] = links[component]
        components.append(record)
    return {"contract": 2, "stack": "observability", "configuredAt": configured_at, "components": components,
            "features": {"backups": {"configured": bool(settings.get("OB_BACKUP_DIR")),
                                     "lastCheckpointAt": last_checkpoint(backups)},
                         "alerts": {"configured": alert_config(settings)[0] != "placeholder"}}}


def write_file(path: Path, text: str, mode: int) -> None:
    """Replace the file whole, so a container reading the mount never sees a partial one."""
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def write_status(state: Path, document: dict) -> None:
    # Caddy reads the mount as another user.
    write_file(console_dir(state) / "status.json", json.dumps(document, indent=2) + "\n", 0o644)


def platform_allocation(settings) -> tuple[str, str]:
    subnet = settings.get("OB_PLATFORM_SUBNET") or PLATFORM_SUBNET
    ip_range = settings.get("OB_PLATFORM_IP_RANGE") or PLATFORM_IP_RANGE
    try:
        network, dynamic = ipaddress.IPv4Network(subnet), ipaddress.IPv4Network(ip_range)
    except ValueError as error:
        raise Refused("invalid_platform_network",
                      "OB_PLATFORM_SUBNET and OB_PLATFORM_IP_RANGE must be IPv4 networks") from error
    if not dynamic.subnet_of(network):
        raise Refused("invalid_platform_network", "OB_PLATFORM_IP_RANGE must lie inside OB_PLATFORM_SUBNET")
    # Docker could hand a trusted address to any container attached to the network.
    for proxy in settings.get("OB_TRUSTED_PROXIES", "").split():
        try:
            trusted = ipaddress.ip_network(proxy, strict=False)
        except ValueError:
            continue
        if trusted.version == 4 and trusted.overlaps(dynamic):
            raise Refused("invalid_platform_network",
                          f"OB_PLATFORM_IP_RANGE {dynamic} must exclude trusted proxy {proxy}")
    return str(network), str(dynamic)


def ensure_network(runner: Runner, name: str, subnet: str, ip_range: str) -> None:
    inspect = ["docker", "network", "inspect", "--format", "{{json .IPAM.Config}}", name]
    probe = runner(inspect)
    if probe.returncode != 0:
        gateway = str(next(ipaddress.IPv4Network(subnet).hosts()))
        created = runner(["docker", "network", "create", "--driver", "bridge", "--subnet", subnet,
                          "--ip-range", ip_range, "--gateway", gateway, name])
        if created.returncode == 0:
            return
        # Another bootstrap may have created it first; validate that network instead.
        probe = runner(inspect)
        if probe.returncode != 0:
            raise Refused("network_create_failed", created.stderr.strip())
    try:
        configs = json.loads(probe.stdout) or []
    except ValueError:
        configs = []
    observed = [(config.get("Subnet", ""), config.get("IPRange", "")) for config in configs]
    # A second IPv4 pool would also hand out addresses; IPv6 pools are left to the operator.
    ipv4 = [entry for entry in observed if ":" not in entry[0]]
    if ipv4 != [(subnet, ip_range)]:
        found = "; ".join(f"subnet {s or 'none'} ip-range {r or 'none'}" for s, r in observed) or "no IPAM configuration"
        raise Refused("platform_network_mismatch",
                      f"network {name} has {found}; expected subnet {subnet} ip-range {ip_range}. "
                      f"One-time fix: stop every stack on {name}, run `docker network rm {name}`, "
                      "then rerun bootstrap")


def ensure_volumes(runner: Runner, prefix: str, project: str, s3: bool = False) -> None:
    for key in VOLUMES:
        if key == "rustfs-data" and not s3:
            continue
        name = f"{prefix}_{key}"
        result = runner(["docker", "volume", "create", "--label",
                         f"com.docker.compose.project={project}", name])
        if result.returncode:
            raise Refused("volume_create_failed", name)


def alert_config(settings: dict[str, str]) -> tuple[str, dict[str, str]]:
    if settings.get("OB_ALERT_WEBHOOK_URL"):
        url = urllib.parse.urlsplit(settings["OB_ALERT_WEBHOOK_URL"])
        if url.scheme not in ("http", "https") or not url.hostname:
            raise Refused("alert_delivery_invalid", "OB_ALERT_WEBHOOK_URL must be an HTTP(S) URL")
        return "webhook", {}
    email = "" if settings.get("OB_ALERT_EMAIL", "").endswith("@example.invalid") else settings.get("OB_ALERT_EMAIL", "")
    if bool(email) != bool(settings.get("OB_SMTP_URL")):
        # Half an email route is a typo, not a choice to run without delivery.
        raise Refused("alert_delivery_invalid", "set both OB_ALERT_EMAIL and OB_SMTP_URL, or neither")
    if email:
        url = urllib.parse.urlsplit(settings["OB_SMTP_URL"])
        if url.scheme not in ("smtp", "smtps") or not url.hostname:
            raise Refused("alert_delivery_invalid", "OB_SMTP_URL must be smtp[s]://[user:password@]host:port")
        try:
            port = url.port or (465 if url.scheme == "smtps" else 587)
        except ValueError as error:
            raise Refused("alert_delivery_invalid", "invalid SMTP port") from error
        host = f"[{url.hostname}]" if ":" in url.hostname else url.hostname
        smtp = {
            "enabled": "true", "host": f"{host}:{port}",
            "user": urllib.parse.unquote(url.username or ""),
            "password": urllib.parse.unquote(url.password or ""),
            "from_address": settings["OB_ALERT_EMAIL"],
            "startTLS_policy": "MandatoryStartTLS" if url.scheme == "smtp" else "NoStartTLS",
        }
        for value in smtp.values():
            if any(c in value for c in ('\n', '\r', '"""')):
                raise Refused("alert_delivery_invalid", "SMTP values must be single-line without triple quotes")
        return "email", smtp
    # No delivery configured: start anyway and report degraded until an operator adds one.
    return "placeholder", {}


def grafana_ini(settings: dict[str, str]) -> str:
    _, smtp = alert_config(settings)
    return "[smtp]\n" + "".join(f'{key} = """{value}"""\n' for key, value in smtp.items())


def write_provisioning(root: Path, state: Path, settings: dict[str, str]) -> str:
    kind, _ = alert_config(settings)
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(state, 0o700)
    provisioning = state / "grafana-provisioning"
    shutil.copytree(root / "docker/grafana/provisioning", provisioning, dirs_exist_ok=True)
    contact = {"apiVersion": 1, "contactPoints": [{"orgId": 1, "name": "configure-delivery",
               "receivers": [{"uid": "configure-delivery", "type": "email" if kind == "placeholder" else kind,
                              "disableResolveMessage": False, "settings":
                              {"url": "$OB_ALERT_WEBHOOK_URL"} if kind == "webhook" else
                              {"addresses": "$OB_ALERT_EMAIL" if kind == "email" else "configure@example.invalid"}}]}],
               "policies": [{"orgId": 1, "receiver": "configure-delivery", "group_by": ["grafana_folder", "alertname"]}]}
    (provisioning / "alerting/contact-points.yaml").write_text(json.dumps(contact, indent=2) + "\n")
    # Compose file secrets keep host ownership, and Grafana runs as uid 472 in group 0, so the
    # files are 0644 and the 0700 directory keeps other host users out.
    secrets_dir = state / "secrets"
    secrets_dir.mkdir(mode=0o700, exist_ok=True)
    os.chmod(secrets_dir, 0o700)
    write_file(secrets_dir / "grafana-admin", settings["OB_GRAFANA_ADMIN_PASSWORD"], 0o644)
    write_file(secrets_dir / "grafana.ini", grafana_ini(settings), 0o644)
    (state / "grafana.ini").unlink(missing_ok=True)
    marker = console_dir(state) / "alerts-degraded.json"
    if kind == "placeholder":
        write_file(marker, '{"status":"degraded","problem":"alert_delivery_placeholder"}\n', 0o644)
    else:
        marker.unlink(missing_ok=True)
    return kind


def local_origin(settings: dict[str, str], scheme: str | None = None) -> str:
    scheme = scheme or ("https" if settings.get("OB_ACCESS_MODE") == "public" else "http")
    port = settings.get("OB_HTTPS_PORT" if scheme == "https" else "OB_HTTP_PORT") or ("443" if scheme == "https" else "80")
    return f"{scheme}://127.0.0.1:{port}"


def grafana_origin(settings: dict[str, str]) -> str:
    return settings.get("OB_GRAFANA_URL") or (
        f'{settings.get("OB_SCHEME", "http")}://'
        f'{settings.get("OB_GRAFANA_HOST", "grafana.localhost")}'
        f'{settings.get("OB_PUBLIC_PORT_SUFFIX", "")}')


def rustfs_origin(settings: dict[str, str]) -> str:
    return settings.get("OB_RUSTFS_URL") or (
        f'{settings.get("OB_SCHEME", "http")}://'
        f'{settings.get("OB_RUSTFS_HOST", "rustfs.localhost")}'
        f'{settings.get("OB_PUBLIC_PORT_SUFFIX", "")}')


def browser_url_config(settings: dict[str, str], app: str) -> None:
    prefix = "OB_" + app.upper()
    # Validate before urlsplit, which silently strips some whitespace characters.
    origin = settings.get(prefix + "_URL", "")
    settings[prefix + "_URL"] = origin
    settings[prefix + "_URL_HOST"] = ""
    settings[prefix + "_AUTHORITY"] = ""
    if not origin:
        return
    try:
        if re.search(r"[\s/?#@\\]", origin.removeprefix("https://").removeprefix("http://")):
            raise ValueError
        url = urllib.parse.urlsplit(origin)
        host = url.hostname or ""
        if url.scheme not in ("http", "https") or not url.netloc or url.path or url.query or url.fragment:
            raise ValueError
        if url.netloc.startswith("["):
            if not re.fullmatch(r"[0-9a-f:.]+", host):
                raise ValueError
            ipaddress.IPv6Address(host)
            authority_host = "[" + host + "]"
        else:
            if len(host) > 253 or not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                                          for label in host.split(".")):
                raise ValueError
            authority_host = host
        port = url.port
        if port is not None and not 1 <= port <= 65535:
            raise ValueError
        authority = authority_host + (":" + str(port) if port is not None else "")
        if url.netloc.lower() != authority:
            raise ValueError
        if settings["OB_ACCESS_MODE"] == "public" and url.scheme != "https":
            raise ValueError
    except ValueError as error:
        raise Refused(app + "_url_invalid", prefix + "_URL must be an HTTP(S) origin with no path, "
                      "credentials, query or fragment; public mode requires HTTPS") from error
    if port == (443 if url.scheme == "https" else 80):
        authority = authority_host
    settings[prefix + "_URL"] = url.scheme + "://" + authority
    settings[prefix + "_URL_HOST"] = host
    settings[prefix + "_AUTHORITY"] = authority


def access_config(settings: dict[str, str]) -> None:
    mode = settings.get("OB_ACCESS_MODE") or "local"
    if mode not in ("local", "public", "proxy"):
        raise Refused("access_mode_invalid", "OB_ACCESS_MODE must be local, public or proxy")
    settings["OB_ACCESS_MODE"] = mode
    settings["OB_SCHEME"] = settings.get("OB_SCHEME") or ("http" if mode == "local" else "https")
    if (settings["OB_SCHEME"] not in ("http", "https") or
            (mode == "public" and settings["OB_SCHEME"] != "https")):
        raise Refused("access_mode_conflict", "OB_SCHEME must be http or https; public mode requires https")
    settings["OB_TLS_ISSUER"] = tls_issuer(mode, settings.get("OB_TLS_ISSUER", ""))
    # Compose interpolates the raw value, so only names with a Caddy snippet may pass.
    if settings["OB_TLS_ISSUER"] not in {"local": ("internal", "files"), "public": ("acme", "files"), "proxy": ("",)}[mode]:
        raise Refused("invalid_settings", "OB_TLS_ISSUER must be internal or files in local mode and acme or files in public mode")
    domain = settings.get("OB_PUBLIC_DOMAIN") or "localhost"
    settings["OB_PUBLIC_DOMAIN"] = domain
    try:
        ipaddress.ip_address(domain)
        ip_root = True
    except ValueError:
        ip_root = False
    settings["OB_GRAFANA_HOST"] = settings.get("OB_GRAFANA_HOST") or ("grafana.localhost" if ip_root else "grafana." + domain)
    settings["OB_RUSTFS_HOST"] = settings.get("OB_RUSTFS_HOST") or ("rustfs.localhost" if ip_root else "rustfs." + domain)
    # Disabled RustFS still reserves an HTTP site returning 404. Validate that host too.
    hosts = (domain, settings["OB_GRAFANA_HOST"], settings["OB_RUSTFS_HOST"])
    for host in hosts:
        if len(host) > 253 or any(not re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", label)
               for label in host.split(".")):
            raise Refused("access_host_invalid", "root and application hosts must be explicit DNS names or an IPv4 root")
    if len(set(host.lower() for host in hosts)) != len(hosts):
        raise Refused("access_host_invalid", "applications require separate internal hostnames")
    if mode == "public":
        public_hosts = (domain, settings["OB_GRAFANA_HOST"])
        if settings.get("OB_RUSTFS_CONSOLE") == "true":
            public_hosts += (settings["OB_RUSTFS_HOST"],)
        for host in public_hosts:
            try:
                ipaddress.ip_address(host)
                public_ip = True
            except ValueError:
                public_ip = False
            if public_ip or "." not in host or host.endswith(".localhost"):
                raise Refused("access_host_invalid", "public mode requires public DNS hostnames")
    suffix = settings.get("OB_PUBLIC_PORT_SUFFIX", "")
    if suffix and (not re.fullmatch(r":[0-9]+", suffix) or not 1 <= int(suffix[1:]) <= 65535):
        raise Refused("access_port_invalid", "OB_PUBLIC_PORT_SUFFIX must be empty or :port")
    for key in ("OB_HTTP_PORT", "OB_HTTPS_PORT"):
        port = settings.get(key, "80" if key == "OB_HTTP_PORT" else "443")
        if not port.isdigit() or not 1 <= int(port) <= 65535:
            raise Refused("access_port_invalid", key + " must be a port number")
    browser_url_config(settings, "grafana")
    browser_url_config(settings, "rustfs")
    browser_url_config(settings, "platform")
    if (settings["OB_RUSTFS_URL_HOST"] == settings["OB_GRAFANA_HOST"].lower() or
            settings["OB_GRAFANA_URL_HOST"] == settings["OB_RUSTFS_HOST"].lower()):
        raise Refused("rustfs_origin_conflict", "browser origins must not reuse another application's internal hostname")
    enabled = settings.get("OB_RUSTFS_CONSOLE") or "false"
    if enabled not in ("true", "false"):
        raise Refused("rustfs_console_invalid", "OB_RUSTFS_CONSOLE must be true or false")
    settings["OB_RUSTFS_CONSOLE"] = enabled
    console_allow = settings.get("OB_RUSTFS_CONSOLE_ALLOW", "127.0.0.1/8 ::1")
    try:
        if not console_allow.strip():
            raise ValueError
        for address in console_allow.split():
            if ipaddress.ip_network(address, strict=False).prefixlen == 0:
                raise ValueError
    except ValueError as error:
        raise Refused("rustfs_console_allow_invalid",
                      "OB_RUSTFS_CONSOLE_ALLOW requires client IPs or CIDRs narrower than all addresses") from error
    settings["OB_RUSTFS_CONSOLE_ALLOW"] = console_allow
    if enabled == "true" and "s3" not in settings.get("COMPOSE_PROFILES", "").split(","):
        raise Refused("rustfs_console_requires_s3", "enable the console only on an existing S3 installation; "
                      "storage changes require an explicit migration")
    # Proxy routing compares authorities; the scheme cannot distinguish routes.
    origins = (rustfs_origin(settings), grafana_origin(settings),
               settings["OB_SCHEME"] + "://" + domain + suffix)
    urls = [urllib.parse.urlsplit(origin) for origin in origins]
    authorities = [url.netloc.lower().removesuffix(":443" if url.scheme == "https" else ":80")
                   for url in urls]
    if authorities[0] in authorities[1:]:
        raise Refused("rustfs_origin_conflict", "RustFS requires a separate browser authority")
    # Compose renders the same default for an empty value.
    settings["OB_TRUSTED_PROXIES"] = settings.get("OB_TRUSTED_PROXIES") or EDGE_PROXY
    for peer in settings["OB_TRUSTED_PROXIES"].split():
        try:
            network = ipaddress.ip_network(peer, strict=True)
            if network.num_addresses != 1:
                raise ValueError
        except ValueError as error:
            raise Refused("proxy_trust_invalid", "trust only exact proxy IPs or /32 and /128 host routes") from error


def tls_issuer(mode: str, configured: str) -> str:
    """The effective issuer; behind another gateway there is no HTTPS listener, so it is empty."""
    if mode == "proxy":
        return ""
    return configured or {"public": "acme"}.get(mode, "internal")


def tls_hostnames(settings: dict[str, str]) -> list[str]:
    """Names of the HTTPS sites; 127.0.0.1 keeps the internal CA and configured origins add no names."""
    hosts = [settings["OB_PUBLIC_DOMAIN"], settings["OB_GRAFANA_HOST"]]
    return hosts + ([settings["OB_RUSTFS_HOST"]] if settings.get("OB_RUSTFS_CONSOLE") == "true" else [])


def certificate_covers(names: set[str], host: str) -> bool:
    return host in names or ("." in host and "*." + host.split(".", 1)[1] in names)


def mounted_tls_files(settings: dict[str, str]) -> list[str]:
    """Container paths of operator certificate inputs mounted by the selected overlays."""
    if settings["OB_TLS_ISSUER"] == "files":
        return ["/certs/tls.crt", "/certs/tls.key"]
    if settings["OB_TLS_ISSUER"] == "acme" and settings.get("OB_ACME_CA_ROOT"):
        return ["/certs/acme-ca-root.crt"]
    return []


def trust_files(settings: dict[str, str]) -> list[str]:
    """Settings naming CA files the effective issuer uses."""
    issuer = settings["OB_TLS_ISSUER"]
    return [key for key, used in (("OB_TLS_CA", issuer in {"acme", "files"}), ("OB_ACME_CA_ROOT", issuer == "acme"))
            if used and settings.get(key)]


def check_tls_inputs(runner: Runner, settings: dict[str, str], root: Path) -> None:
    issuer = settings["OB_TLS_ISSUER"]
    if issuer == "files" and not settings.get("OB_TLS_DIR"):
        raise Refused("invalid_settings", "OB_TLS_ISSUER=files needs OB_TLS_DIR, a directory holding tls.crt and tls.key")
    if issuer == "files":
        try:
            ipaddress.ip_address(settings["OB_PUBLIC_DOMAIN"])
        except ValueError:
            pass
        else:
            # 127.0.0.1 keeps the internal CA, so an IP root would be defined twice.
            raise Refused("invalid_settings", "OB_TLS_ISSUER=files needs a DNS name in OB_PUBLIC_DOMAIN, not an IP root")
    if issuer == "acme":
        if settings.get("OB_ACME_CA") and not re.fullmatch(r"https://[^/\s]+(?:/\S*)?", settings["OB_ACME_CA"]):
            raise Refused("invalid_settings", "OB_ACME_CA must be an https:// ACME directory URL")
        if bool(settings.get("OB_ACME_EAB_KEY_ID")) != bool(settings.get("OB_ACME_EAB_HMAC")):
            raise Refused("invalid_settings", "OB_ACME_EAB_KEY_ID and OB_ACME_EAB_HMAC must be set together")
        # trusted_roots replaces Caddy's trust pool for the ACME server; the public default would fail.
        if settings.get("OB_ACME_CA_ROOT") and not settings.get("OB_ACME_CA"):
            raise Refused("invalid_settings", "OB_ACME_CA_ROOT needs OB_ACME_CA, the private ACME directory it trusts")
    for key in trust_files(settings):
        path = root / settings[key]
        try:
            if not path.is_file():
                raise OSError("not a regular file")
            ssl.create_default_context(cadata=path.read_text(encoding="utf-8"))
        except (OSError, ValueError, ssl.SSLError) as error:
            raise Refused("invalid_settings", f"{key} ({path}) must be a readable PEM file holding CA certificates") from error
    if issuer != "files":
        return
    directory = root / settings["OB_TLS_DIR"]
    certificate = directory / "tls.crt"
    if not directory.is_dir() or not certificate.is_file() or not (directory / "tls.key").is_file():
        raise Refused("invalid_settings", f"OB_TLS_DIR ({directory}) must be a directory holding tls.crt and tls.key")
    if shutil.which("openssl") is None:
        raise Refused("openssl_missing", "install openssl; bootstrap reads the certificate's subject alternative names with it")
    result = runner(["openssl", "x509", "-in", str(certificate), "-noout", "-ext", "subjectAltName"])
    if result.returncode:
        raise Refused("invalid_settings", f"openssl cannot read {certificate} as a PEM certificate")
    names = {name.lower() for name in SAN_NAME.findall(result.stdout)}
    missing = [host for host in tls_hostnames(settings) if not certificate_covers(names, host.lower())]
    if missing:
        raise Refused("invalid_settings", f"{certificate} does not cover {', '.join(missing)}; its subject alternative names are "
                      + (", ".join(sorted(names)) or "empty"))


def check_tls_files_readable(runner: Runner, settings: dict[str, str], command: list[str]) -> None:
    """Read the mounted certificate inputs in a throwaway Caddy container before starting."""
    mounted = mounted_tls_files(settings)
    if not mounted:
        return
    # Off every network: a one-off Caddy must never answer as ob-gateway on the Platform Network.
    with tempfile.NamedTemporaryFile("w", suffix=".yaml") as isolated:
        isolated.write("services:\n  caddy:\n    networks: !reset []\n    network_mode: none\n")
        isolated.flush()
        result = runner(command + ["-f", isolated.name, "run", "--rm", "--no-deps", "-T", "--entrypoint", "sh",
                                   "caddy", "-ec", f"cat {' '.join(mounted)} >/dev/null"])
    if result.returncode:
        raise Refused("tls_files_unreadable", "Caddy (uid 0 without CAP_DAC_OVERRIDE) cannot read "
                      + ", ".join(mounted) + "; own tls.key by root with mode 0600, and keep certificates readable: "
                      + (result.stderr or result.stdout).strip()[-500:])


def compose_selection(root: Path, settings: dict[str, str], s3: bool, proxy: bool) -> list[str]:
    defaults = ["compose.yaml"] + (["compose.s3.yaml"] if s3 else []) + (["compose.proxy.yaml"] if proxy else [])
    # A recorded TLS overlay from an earlier issuer would demand its unused input.
    managed = {root / name for name in TLS_OVERLAYS}
    saved = ":".join(name for name in settings.get("COMPOSE_FILE", "").split(":")
                     if not name or (root / name).resolve() not in managed)
    generated = {":".join(["compose.yaml"] + extra) for extra in
                 ([], ["compose.s3.yaml"], ["compose.proxy.yaml"], ["compose.s3.yaml", "compose.proxy.yaml"])}
    # Keep generated mode selection compatible; preserve every custom overlay in order.
    files = defaults if not saved or saved in generated else saved.split(":")
    resolved = [(root / name).resolve() for name in files]
    if (not files or resolved[0] != root / "compose.yaml" or
            any(not name or any(c in name for c in "\n\r$`") for name in files) or
            len(set(resolved)) != len(resolved) or
            ((root / "compose.s3.yaml") in resolved) != s3 or
            ((root / "compose.proxy.yaml") in resolved) != proxy):
        raise Refused("compose_file_conflict", "retain the base first and overlays matching the selected storage and access modes")
    if any(not path.is_file() for path in resolved):
        raise Refused("compose_file_conflict", "a selected Compose file is missing")
    issuer = settings.get("OB_TLS_ISSUER", "")
    overlays = ["compose.files.yaml"] if issuer == "files" else []
    if issuer == "acme":
        overlays += [name for name, key in (("compose.acme-ca-root.yaml", "OB_ACME_CA_ROOT"),
                                            ("compose.acme-eab.yaml", "OB_ACME_EAB_KEY_ID")) if settings.get(key)]
    # After the stack's storage and mode files, so operator overlays still apply last.
    stack = {root / name for name in ("compose.yaml", "compose.s3.yaml", "compose.proxy.yaml")}
    at = max(index for index, path in enumerate(resolved) if path in stack) + 1
    files = files[:at] + [str(Path(files[at - 1]).with_name(name)) for name in overlays] + files[at:]
    selected = ":".join(files)
    if os.environ.get("COMPOSE_FILE") and os.environ["COMPOSE_FILE"] != selected:
        raise Refused("compose_file_conflict", "unset COMPOSE_FILE or record the same selection in .env")
    settings["COMPOSE_FILE"] = selected
    return files


def assignments(lines: list[str]) -> dict[str, str]:
    return {m.group("key"): unquote(m.group("value")) for m in map(ENV_LINE.match, lines) if m}


def resolve_settings(lines: list[str], template: Path) -> dict[str, str]:
    """The env file over the template, then the shell, validated, plus every derived value."""
    settings = assignments(template.read_text(encoding="utf-8").splitlines()) | assignments(lines)
    settings.update({key: value for key, value in os.environ.items()
                     if key.startswith("OB_") or key in ("COMPOSE_PROFILES", "COMPOSE_PROJECT_NAME")})
    access_config(settings)
    settings["OB_PLATFORM_SUBNET"], settings["OB_PLATFORM_IP_RANGE"] = platform_allocation(settings)
    settings["COMPOSE_PROJECT_NAME"] = project_name(settings)
    scheme = settings["OB_SCHEME"]
    default_port = "443" if scheme == "https" else "80"
    port = settings.get("OB_HTTPS_PORT" if scheme == "https" else "OB_HTTP_PORT") or default_port
    if settings["OB_ACCESS_MODE"] != "proxy" and not settings.get("OB_PUBLIC_PORT_SUFFIX") and port != default_port:
        settings["OB_PUBLIC_PORT_SUFFIX"] = ":" + port
    grafana_ini(settings)
    return settings


def migrate_env(lines: list[str], template: Path) -> list[str]:
    """Drop values an earlier bootstrap saved in the env file that are now derived."""
    def keep(line: str) -> bool:
        match = ENV_LINE.match(line)
        return not match or not (match.group("key") in DERIVED_ONLY or (
            match.group("key") == "COMPOSE_PROJECT_NAME" and unquote(match.group("value")) == PROJECT))
    lines = [line for line in lines if keep(line)]
    suffix = assignments(lines).get("OB_PUBLIC_PORT_SUFFIX", "")
    without = [line for line in lines if not (ENV_LINE.match(line) and
                                              ENV_LINE.match(line).group("key") == "OB_PUBLIC_PORT_SUFFIX")]
    if suffix and resolve_settings(without, template)["OB_PUBLIC_PORT_SUFFIX"] == suffix:
        return without
    return lines


def derived_env(env_file: Path) -> Path:
    return env_file.parent / "data" / "derived.env"


def write_derived(env_file: Path, settings: dict[str, str]) -> None:
    path = derived_env(env_file)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Compose cannot see a file secret change, so this value recreates Grafana when the SMTP
    # settings change. It is keyed, so Grafana's environment reveals nothing about the password.
    revision = hmac.new(settings["OB_GRAFANA_ADMIN_PASSWORD"].encode(), grafana_ini(settings).encode(),
                        "sha256").hexdigest()
    write_file(path, "# Written by scripts/bootstrap.py from " + env_file.name + "; edit that file and rerun bootstrap.\n"
               + "".join(f"{key}={settings.get(key, '')}\n" for key in SAVED)
               + f"OB_GRAFANA_INI_HMAC={revision}\n", 0o600)


def sync_shell(settings: dict[str, str]) -> None:
    """The shell overrides env files in Compose; keep it consistent with the resolved values."""
    for key in SAVED:
        if key in os.environ:
            os.environ[key] = settings[key]


def env_files(env_file: Path) -> list[str]:
    """Compose reads the operator's env file, then bootstrap's derived values over it."""
    return ["--env-file", str(env_file), "--env-file", str(derived_env(env_file))]


def compose_up(root: Path, env_file: Path, runner: Runner, s3: bool = False, proxy: bool = False,
               files: list[str] | None = None) -> None:
    # Fifteen minutes includes cold image pulls; a timeout preserves cached layers for retry.
    result = runner([
        "docker", "compose", "--project-directory", str(root), *env_files(env_file),
        *[arg for name in (files or (["compose.yaml"] + (["compose.s3.yaml"] if s3 else []) +
                                    (["compose.proxy.yaml"] if proxy else []))) for arg in ("-f", str(root / name))],
        *(["--profile", "s3"] if s3 else []),
        "up", "--detach", "--wait", "--wait-timeout", "300",
    ], timeout=900)
    if result.returncode != 0:
        raise Refused("compose_up_failed", (result.stderr or result.stdout).strip()[-2000:])


def compose_services(runner: Runner, command: list[str]) -> dict:
    result = runner(command + ["config", "--format", "json"])
    if result.returncode:
        raise Refused("compose_config_failed", "inspect Compose configuration privately")
    return json.loads(result.stdout)["services"]


class LocalHTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        # Dial loopback while verifying the certificate and sending SNI for the public host.
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


def wait_ready(url: str, timeout: float = 120.0, host: str | None = None, ca_data: str | None = None) -> None:
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        try:
            request = urllib.request.Request(url, headers={"Host": host} if host else {})
            if host and url.startswith("https://127.0.0.1:"):
                target = urllib.parse.urlsplit(url)
                options = {"context": ssl.create_default_context(cadata=ca_data)} if ca_data else {}
                connection = LocalHTTPSConnection(host, port=target.port, timeout=5, **options)
                try:
                    connection.request("GET", target.path, headers={"Host": host})
                    status = connection.getresponse().status
                finally:
                    connection.close()
            else:
                with urllib.request.urlopen(request, timeout=5) as response:
                    status = response.status
            if status == 200:
                return
            last = f"http {status}"
        except (urllib.error.URLError, OSError, http.client.HTTPException) as error:
            last = str(error)
        time.sleep(3)
    raise Refused("not_ready", f"{url}: {last}")


def probe_trust(settings: dict[str, str], runner: Runner, command: list[str]) -> str:
    """PEM data the HTTPS probe trusts; empty means the system store."""
    if settings["OB_TLS_ISSUER"] == "internal":
        certificate = runner(command + ["exec", "-T", "caddy", "cat", "/data/caddy/pki/authorities/local/root.crt"])
        if certificate.returncode:
            raise Refused("local_ca_unavailable", "cannot read this installation's public CA certificate")
        return certificate.stdout
    root = Path(__file__).resolve().parent.parent
    for key in trust_files(settings):
        try:
            return (root / settings[key]).read_text(encoding="utf-8")
        except OSError as error:
            raise Refused("invalid_settings", f"{key} ({root / settings[key]}) is not readable") from error
    return ""


# Bound bootstrap commands without changing the shared runner used for bulk checkpoint I/O.
def bootstrap(argv: list[str], runner: Runner = partial(run, timeout=60)) -> int:
    parser = argparse.ArgumentParser(prog="bootstrap.py", description=__doc__.splitlines()[0])
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--template", default=".env.example")
    parser.add_argument("--render-only", action="store_true",
                        help="write the env file, start nothing")
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parent.parent
    env_file = (root / args.env_file).resolve()
    template = (root / args.template).resolve()

    installation = env_file == (root / ".env").resolve()
    if shutil.which("docker") is None and (installation or not args.render_only):
        raise Refused("docker_missing", "install Docker with the Compose plugin")

    lock_path = env_file.with_name(env_file.name + ".lock")
    with open(lock_path, "w", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise Refused("bootstrap_already_running", str(lock_path)) from error

        lines, present = read_env(env_file)
        # Compose lets the shell override the env file. A shell value that differs from
        # the saved one would run the stack with a secret the file does not record.
        conflicts = sorted(k for k in MANAGED if k in present and k in os.environ and os.environ[k] != present[k])
        if conflicts:
            raise Refused("shell_env_conflict", "unset in the shell or fix .env: " + ", ".join(conflicts))
        missing = MANAGED - set(present)
        original = lines
        if not lines:
            lines = template.read_text(encoding="utf-8").splitlines()
            lines += ["", "# Generated by scripts/bootstrap.py. Keep this file private and backed up."]
        lines = migrate_env(lines, template)
        settings = resolve_settings(lines, template)
        s3 = "s3" in settings.get("COMPOSE_PROFILES", "").split(",")
        proxy = settings["OB_ACCESS_MODE"] == "proxy"
        files = compose_selection(root, settings, s3, proxy)
        check_tls_inputs(runner, settings, root)
        project = settings["COMPOSE_PROJECT_NAME"]
        prefix = settings.get("OB_VOLUME_PREFIX") or PROJECT
        state_dir = Path(settings.get("OB_STATE_DIR", "./data"))
        if not state_dir.is_absolute():
            state_dir = root / state_dir
        data_dir = state_dir / "installation"
        mode = "s3" if s3 else "filesystem"
        marker = data_dir / "storage-mode"
        if not args.render_only and marker.exists() and marker.read_text().strip() != mode:
            raise Refused("storage_migration_required", "storage mode differs from the installation; restore or migrate explicitly")

        # Scratch render-only stays offline. Installation bootstrap checks volumes
        # even when all secrets are present, before writing any installation state.
        existing = []
        if installation or not args.render_only:
            existing = installation_state(root, data_dir, runner, project, prefix)
            if not (data_dir / "storage-mode").exists() and any(item.startswith("volume ") for item in existing):
                raise Refused("storage_mode_unknown",
                              f"project volumes exist without {data_dir / 'storage-mode'}; verify the prior storage mode, "
                              "then record filesystem or s3 in that file (see docs/operations/backup.md)")

        if missing and existing:
            raise Refused(
                "existing_installation_missing_secrets",
                "restore the original .env before starting; found " + "; ".join(existing),
            )
        fresh = generate(missing)
        # A secret supplied in the shell on a fresh install is the operator's choice;
        # record it instead of generating a different one.
        for key in missing:
            value = os.environ.get(key, "")
            if value:
                # The same rule read_env applies when it reads the value back.
                if unquote(value) != value or any(c in value for c in "$\n\r"):
                    raise Refused("env_repair_required", f"{key} from the shell must be a literal single-line value")
                fresh[key] = value
        settings.update(present | fresh)
        # The env file records explicit shell choices and the Compose file selection, so later
        # Compose and Checkpoint commands resolve the same installation. Nothing else is rewritten.
        recorded = {key: os.environ[key] for key in SAVED if key in os.environ and key not in DERIVED_ONLY}
        recorded["COMPOSE_FILE"] = settings["COMPOSE_FILE"]
        if "COMPOSE_PROFILES" in os.environ:
            recorded["COMPOSE_PROFILES"] = settings["COMPOSE_PROFILES"]
        for key, value in recorded.items():
            found = False
            for index, line in enumerate(lines):
                match = ENV_LINE.match(line)
                if match and match.group("key") == key:
                    if unquote(match.group("value")) != value:
                        lines[index] = f"{key}={value}"
                    found = True
            if not found:
                lines.append(f"{key}={value}")
        lines += [f"{key}={value}" for key, value in fresh.items()]
        if lines != original:
            write_env(env_file, lines)
        write_derived(env_file, settings)
        sync_shell(settings)
        if args.render_only:
            print(json.dumps({"env": str(env_file), "project": project, "generated": sorted(missing)}))
            return 0

        configured_at = utc(datetime.now(timezone.utc))
        # Profiles cannot replace another service's config mount. Record the matching
        # override so later plain docker compose commands use the same storage mode.
        data_dir.mkdir(parents=True, exist_ok=True)
        marker.write_text(mode + "\n")
        (state_dir / "textfile").mkdir(parents=True, exist_ok=True)
        backup_dir = Path(settings.get("OB_BACKUP_DIR", "./data/backups"))
        if not backup_dir.is_absolute():
            backup_dir = root / backup_dir
        backup_dir.mkdir(parents=True, exist_ok=True)
        command = ["docker", "compose", "--project-directory", str(root), *env_files(env_file),
                   *[arg for name in files for arg in ("-f", str(root / name))]]
        # Every service, then those the selected profiles enable.
        document = status_document(compose_services(runner, command + ["--profile", "*"]),
                                   compose_services(runner, command + (["--profile", "s3"] if s3 else [])),
                                   settings, backup_dir, configured_at)
        write_links(state_dir, settings)
        delivery = write_provisioning(root, state_dir, settings)

        ensure_network(runner, settings.get("OB_PLATFORM_NETWORK", NETWORK),
                       settings["OB_PLATFORM_SUBNET"], settings["OB_PLATFORM_IP_RANGE"])
        ensure_volumes(runner, prefix, project, s3)
        check_tls_files_readable(runner, settings, command + (["--profile", "s3"] if s3 else []))
        compose_up(root, env_file, runner, s3, proxy, files)
        scheme = settings.get("OB_SCHEME", "http")
        domain = settings.get("OB_PUBLIC_DOMAIN", "localhost")
        origin = domain + settings.get("OB_PUBLIC_PORT_SUFFIX", "")
        services = ("grafana", "loki", "tempo", "mimir", "alloy")
        if settings["OB_ACCESS_MODE"] != "public":
            for service in services:
                wait_ready(f"{local_origin(settings)}/health/{service}", host=domain)
        if settings["OB_ACCESS_MODE"] != "proxy":
            trusted = probe_trust(settings, runner, command)
            probes = [(f"/health/{service}", domain) for service in services]
            if settings["OB_ACCESS_MODE"] == "public":
                probes.append(("/login", settings["OB_GRAFANA_HOST"]))
            try:
                for path, host in probes:
                    wait_ready(local_origin(settings, "https") + path, host=host, ca_data=trusted)
            except Refused as refused:
                if ("CERTIFICATE_VERIFY_FAILED" in refused.detail and settings["OB_TLS_ISSUER"] != "internal"
                        and not settings.get("OB_TLS_CA")):
                    refused.detail += "; set OB_TLS_CA to the issuing CA's PEM file when it is not in the system trust store"
                raise
        write_status(state_dir, document)
        print(json.dumps({
            "status": "degraded" if delivery == "placeholder" else "ready",
            "problems": ["alert_delivery_placeholder"] if delivery == "placeholder" else [],
            "console": f"{scheme}://{origin}/",
            "grafana": grafana_origin(settings) + "/",
            "grafanaLogin": "admin",
            "next": "Log in to Grafana with admin and OB_GRAFANA_ADMIN_PASSWORD from .env; open Stacks or Explore.",
        }))
        return 0


def main() -> int:
    try:
        return bootstrap(sys.argv[1:])
    except Refused as refused:
        print(json.dumps({"error": refused.code, "detail": refused.detail}), file=sys.stderr)
        return 3 if refused.code in ("not_ready", "compose_up_failed") else 1
    except subprocess.TimeoutExpired:
        print(json.dumps({"error": "docker_timeout", "detail": "Docker did not finish within the operation deadline; inspect installation state before retrying."}), file=sys.stderr)
        return 3
    except SystemExit as exit_:  # argparse
        return 2 if exit_.code not in (0, None) else 0


if __name__ == "__main__":
    raise SystemExit(main())
