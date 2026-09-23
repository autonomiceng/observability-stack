// Stack Console. Reads /status.json (Status v2, configured images written at bootstrap) and
// polls /health/<service> through the Stack Gateway. No secrets, no writes.
(() => {
  const base = `${location.protocol}//${location.host}`;
  const links = async () => {
    try {
      const response = await fetch(`${base}/links.json`, { cache: "no-store" });
      if (!response.ok) return;
      const configured = await response.json();
      for (const link of document.querySelectorAll("[data-link]")) {
        const origin = configured[link.dataset.link];
        if (/^https?:\/\//.test(origin)) {
          link.href = origin + (link.dataset.path || "/");
        }
      }
      for (const card of document.querySelectorAll("[data-optional-link]")) {
        card.hidden = !/^https?:\/\//.test(configured[card.dataset.optionalLink]);
      }
      for (const link of document.querySelectorAll("[data-external]")) {
        const url = configured[link.dataset.external];
        if (url && /^https?:\/\//.test(url)) link.href = url;
      }
      for (const label of document.querySelectorAll("[data-url]")) {
        const origin = configured[label.dataset.url];
        if (/^https?:\/\//.test(origin)) label.textContent = origin + (label.dataset.path || "");
      }
    } catch {}
  };

  const badge = (li, state, label) => {
    const b = li.querySelector("[data-badge]");
    b.dataset.state = state;
    b.textContent = label;
  };
  // A service this browser has seen healthy is "down" when it stops answering;
  // one it has never seen is "not installed". Optional cards only.
  const seenKey = (s) => `console.seen.${s}`;
  const seen = (s) => localStorage.getItem(seenKey(s)) !== null;
  const remember = (s) => localStorage.setItem(seenKey(s), new Date().toISOString());

  const check = async (li) => {
    const service = li.dataset.service;
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), 4000);
    try {
      const res = await fetch(`${base}/health/${service}`, { signal: ctl.signal, cache: "no-store" });
      if (service === "alerts" && res.status === 503) {
        return badge(li, "degraded", "alert_delivery_placeholder");
      }
      if (res.ok) { remember(service); return badge(li, "ok", "healthy"); }
      if (res.status === 502 || res.status === 503) {
        const absent = li.hasAttribute("data-optional") && !seen(service);
        return badge(li, absent ? "absent" : "down", absent ? "not installed" : "unreachable");
      }
      badge(li, "degraded", `http ${res.status}`);
    } catch (err) {
      badge(li, err.name === "AbortError" ? "stale" : "down",
        err.name === "AbortError" ? "no answer" : "unreachable");
    } finally {
      clearTimeout(timer);
    }
  };

  const checkAll = async () => {
    await Promise.all([...document.querySelectorAll("[data-service]")].map(check));
    const t = new Date();
    document.querySelector("[data-checked]").textContent =
      `Health checked ${t.toLocaleTimeString()}.`;
  };

  const versions = async () => {
    try {
      const res = await fetch(`${base}/status.json`, { cache: "no-store", signal: AbortSignal.timeout(4000) });
      if (!res.ok) throw new Error(String(res.status));
      const v = await res.json();
      if (v.contract !== 2) throw new Error("unsupported status contract");
      const components = new Map((Array.isArray(v.components) ? v.components : []).map((c) => [c.id, c]));
      for (const el of document.querySelectorAll("[data-version]")) {
        const c = components.get(el.dataset.version);
        el.textContent = c?.enabled === false ? "disabled" : (c?.version ?? "unknown");
      }
      const when = document.querySelector("[data-pinned]");
      when.dateTime = v.configuredAt ?? "";
      when.textContent = v.configuredAt ? new Date(v.configuredAt).toLocaleString() : "unknown";
    } catch {
      for (const el of document.querySelectorAll("[data-version]")) el.textContent = "unknown";
      document.querySelector("[data-pinned]").textContent = "unknown";
    }
  };

  links();
  versions();
  checkAll();
  // Repaint only when a check completes; no continuous animation.
  setInterval(checkAll, 30000);
})();
