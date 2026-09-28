// Stack Console. Reads /status.json (Status v2: configured images, enabled state and alert
// delivery), /links.json (browser origins) and probes /health/<id> through the Stack Gateway.
// Badge states and labels follow platform-edge docs/ui-kit.md. No secrets, no writes.
const LABELS = {
  healthy: "Healthy",
  degraded: "Degraded",
  unreachable: "Unreachable",
  unknown: "Unknown",
  configured: "Configured",
  disabled: "Disabled",
};

const probeState = (code) => (code === 200 ? "healthy" : [502, 503, 504].includes(code) ? "unreachable" : "unknown");
// Configuration comes only from the Status Document and reachability only from Health Paths:
// without an enabled entry a component stays Unknown, whatever its probe says.
function componentState(component, probe) {
  if (!component) return "unknown";
  return component.enabled ? probe ?? "unknown" : "disabled";
}
function alertState(status) {
  const alerts = status?.features?.alerts;
  return typeof alerts?.configured !== "boolean" ? "unknown" : alerts.configured ? "configured" : "degraded";
}
const versionText = (component) =>
  !component ? "Version unknown" : component.version ? `Configured ${component.version}` : "Configured";
function parseStatus(doc) {
  if (doc?.contract !== 2 || doc.stack !== "observability" || !Array.isArray(doc.components))
    throw new Error("Unsupported status");
  const valid = (c) => typeof c?.id === "string" && typeof c.enabled === "boolean";
  return { ...doc, components: new Map(doc.components.filter(valid).map((c) => [c.id, c])) };
}
// One check reads the Status Document first and probes only the components it lists as
// enabled. Each check starts empty: an absent or invalid document is unknown, never a
// previous answer.
async function load(getStatus, probe, ids) {
  let status = null;
  try {
    status = parseStatus(await getStatus());
  } catch {}
  const enabled = [...new Set(ids)].filter((id) => status?.components.get(id)?.enabled);
  return { status, health: Object.fromEntries(await Promise.all(enabled.map(async (id) => [id, await probe(id)]))) };
}

if (typeof module !== "undefined")
  module.exports = { probeState, componentState, alertState, versionText, parseStatus, load };

if (typeof document !== "undefined") {
  const $ = (s) => document.querySelector(s);
  const $$ = (s) => [...document.querySelectorAll(s)];
  let checking = false;

  // Live regions announce every write, so text changes only when it differs.
  const text = (element, value) => {
    if (element.textContent !== value) element.textContent = value;
  };
  const badge = (element, state) => {
    element.dataset.state = state;
    text(element, LABELS[state]);
  };
  const utc = (time) => {
    const date = new Date(time);
    return Number.isNaN(date.getTime()) ? "Unknown" : date.toISOString().slice(0, 16).replace("T", " ") + " UTC";
  };
  const request = (path) =>
    fetch(path, { cache: "no-store", credentials: "omit", redirect: "error", signal: AbortSignal.timeout(4000) });
  async function json(path) {
    const response = await request(path);
    if (!response.ok) throw new Error(String(response.status));
    return response.json();
  }
  async function probe(id) {
    try {
      return probeState((await request(`/health/${id}`)).status);
    } catch {
      return "unreachable";
    }
  }
  async function links() {
    const value = await json("/links.json");
    const origin = (name) => (/^https?:\/\/[^/]+$/.test(value[name] || "") ? value[name] : "");
    for (const link of $$("[data-link]")) if (origin(link.dataset.link)) link.href = origin(link.dataset.link) + link.dataset.path;
    for (const card of $$("[data-optional-link]")) card.hidden = !origin(card.dataset.optionalLink);
    // The Edge console is the platform home; a standalone stack shows no link.
    let platform = $("[data-platform]");
    if (origin("platform")) {
      if (!platform) {
        platform = Object.assign(document.createElement("a"), { textContent: "Platform" });
        platform.dataset.platform = "";
        $(".pk-header-links").prepend(platform);
      }
      platform.href = origin("platform") + "/";
    } else platform?.remove();
  }

  function render(status, health) {
    const components = status?.components;
    for (const element of $$("[data-component]")) {
      const component = components?.get(element.dataset.component);
      badge(element.querySelector(".pk-badge"), componentState(component, health[element.dataset.component]));
      const version = element.querySelector("[data-version]");
      text(version, element.matches(".pk-app") ? versionText(component) : component?.version ?? "");
    }
    const alerts = alertState(status);
    badge($("[data-alerts] .pk-badge"), alerts);
    $("[data-alerts-hint]").hidden = alerts !== "degraded";
    const up = Object.values(health).filter((state) => state === "healthy").length;
    // Without a Status Document nothing is known, which is not the same as unreachable.
    text($("[data-summary]"), status ? `${up} of ${Object.keys(health).length} reachable` : "Status unavailable");
    text($("[data-configured-at]"), status ? utc(status.configuredAt) : "Status unavailable");
    const backups = status?.features?.backups;
    text(
      $("[data-backups]"),
      !backups
        ? "Unknown"
        : !backups.configured
          ? "Not configured"
          : backups.lastCheckpointAt
            ? `Configured · last checkpoint ${utc(backups.lastCheckpointAt)}`
            : "Configured · no checkpoint recorded",
    );
  }

  async function check() {
    if (checking) return;
    checking = true;
    $("[data-refresh]").disabled = true;
    text($("[data-refresh]"), "Checking…");
    try {
      const [{ status, health }] = await Promise.all([
        load(() => json("/status.json"), probe, $$("[data-component]").map((element) => element.dataset.component)),
        links().catch(() => {}),
      ]);
      render(status, health);
      text($("[data-checked]"), `Checked ${new Date().toLocaleTimeString()}`);
    } finally {
      $("[data-refresh]").disabled = false;
      text($("[data-refresh]"), "Refresh");
      checking = false;
    }
  }

  text($("[data-scheme]"), location.protocol === "https:" ? "HTTPS" : "HTTP");
  $("[data-refresh]").addEventListener("click", check);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) check();
  });
  // Repaint only when a check completes; no continuous animation.
  setInterval(() => {
    if (!document.hidden) check();
  }, 30000);
  check();
}
