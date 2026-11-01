window.PrivyLeases = (() => {
  "use strict";
  let dialog = null;
  const $ = id => document.getElementById(id);

  function filter() {
    if (!$("leaseq")) return;
    const terms = $("leaseq").value.toLowerCase().split(/\s+/).filter(Boolean);
    const tags = $("leaselabel").value.split(/\s+/).filter(Boolean);
    let shown = 0, total = 0;
    document.querySelectorAll("[data-lease-row], [data-client-row], [data-group-row]").forEach(row => {
      const labelSets = JSON.parse(row.dataset.labelSets || `[${row.dataset.labels || "{}"}]`);
      const match = terms.every(term => (row.dataset.q || "").includes(term)) &&
        (!tags.length || labelSets.some(labels => tags.every(tag => {
          const equal = tag.indexOf("=");
          return equal > 0 && Object.hasOwn(labels, tag.slice(0, equal)) &&
            String(labels[tag.slice(0, equal)]) === tag.slice(equal + 1);
        })));
      row.hidden = !match;
      if (row.hasAttribute("data-lease-row")) { total++; if (match) shown++; }
    });
    $("leasecount").textContent = `${shown} of ${total} leases`;
    $("leasenone").hidden = shown > 0;
    const url = new URL(location.href);
    for (const [name, value] of [["q", $("leaseq").value], ["label", $("leaselabel").value]]) {
      if (value) url.searchParams.set(name, value); else url.searchParams.delete(name);
    }
    history.replaceState(null, "", url);
    document.querySelectorAll("#s-leases .seg a").forEach(anchor => {
      const next = new URL(anchor.href);
      for (const name of ["q", "label"]) {
        if (url.searchParams.has(name)) next.searchParams.set(name, url.searchParams.get(name));
        else next.searchParams.delete(name);
      }
      anchor.href = next.pathname + next.search;
    });
  }

  function element(tag, text, className = "") {
    const node = document.createElement(tag);
    node.textContent = text;
    if (className) node.className = className;
    return node;
  }
  function at(value) {
    if (!value) return "unknown";
    const date = new Date(typeof value === "number" ? value * 1000 : value);
    return Number.isNaN(date.valueOf()) ? String(value) : date.toISOString().replace("T", " ").replace(".000Z", " UTC");
  }
  async function get(path) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 4000);
    try {
      const response = await fetch(path, {
        headers: {"X-Privy-UI": "1"}, cache: "no-store", signal: controller.signal
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error?.message || `Lease lookup failed (${response.status}).`);
      return data;
    } finally { clearTimeout(timer); }
  }
  function confirmLease(lease, target, action, force = false) {
    if (dialog) return Promise.resolve(false);
    const previous = document.activeElement;
    const panel = element("dialog", "", "lease-dialog");
    dialog = panel;
    panel.setAttribute("aria-labelledby", "lease-dialog-title");
    panel.append(element("div", force ? "DASHBOARD ONLY" : "HEADS UP", "eyebrow"));
    const heading = element("h2", force ? `Force release ${lease.lease_id}?` : `${target} is leased by another session`);
    heading.id = "lease-dialog-title";
    panel.append(heading);
    const holder = typeof lease.holder === "object" && lease.holder ? lease.holder : {name: lease.holder};
    panel.append(element("p",
      `${holder.name || lease.client_id || "Unknown holder"} (${holder.kind || "client"}, session ${holder.session_id || "not provided"}) ` +
      `holds group ${lease.group || target} until ${at(lease.expires_at)}.`));
    if (lease.running_jobs?.length) {
      panel.append(element("p", `Pinned by ${lease.running_jobs.length} running job(s); idle TTL ${lease.ttl || ""} starts after the last job ends.`));
    }
    const labels = element("p", "");
    for (const [key, value] of Object.entries(lease.labels || {})) labels.append(element("span", `${key}=${value}`, "lease-tag"));
    panel.append(labels, element("p", `Purpose: ${lease.purpose || "not provided"}`));
    panel.append(element("p", force
      ? "This releases the whole group and skips cooldown. Running jobs keep running; their output remains available."
      : "Your command will still run, but it may disturb their work. It does not renew or take over their lease. It does count as recent use for this group's cooldown.",
    "lease-warning"));
    const actions = element("div", "", "lease-dialog-actions");
    if (lease.lease_id) {
      const peek = element("a", "Peek", "lease-button");
      peek.href = `/ui/leases?state=all&peek=${encodeURIComponent(lease.lease_id)}#s-lease-peek`;
      peek.target = "_blank"; peek.rel = "noopener";
      const logs = element("a", "Peek at their logs", "lease-button");
      logs.href = `/ui/logs?target=*&lease=${encodeURIComponent(lease.lease_id)}`;
      logs.target = "_blank"; logs.rel = "noopener";
      actions.append(peek, logs);
    }
    const cancel = element("button", "Cancel", "lease-button");
    const proceed = element("button", force ? "Force release" : `${action} anyway`, `lease-button ${force ? "danger" : "primary"}`);
    cancel.type = proceed.type = "button";
    actions.append(cancel, proceed);
    panel.append(actions);
    document.body.append(panel);
    return new Promise(resolve => {
      let done = false;
      const finish = result => {
        if (done) return;
        done = true;
        panel.close(); panel.remove(); dialog = null;
        if (previous?.isConnected) previous.focus();
        resolve(result);
      };
      cancel.onclick = () => finish(false);
      proceed.onclick = () => finish(true);
      panel.addEventListener("cancel", event => { event.preventDefault(); finish(false); });
      panel.showModal();
      cancel.focus();
    });
  }
  async function confirmTarget(target, action) {
    let lease = target.lease;
    try {
      const data = await get("/v1/targets");
      const fresh = data.targets.find(item => item.id === target.id);
      if (fresh) lease = fresh.lease;
      $("lease-lookup-error")?.remove();
    } catch (error) {
      let notice = $("lease-lookup-error");
      if (!notice) {
        notice = element("p", "", "lease-action-error");
        notice.id = "lease-lookup-error"; notice.setAttribute("role", "alert");
        (document.querySelector(".main") || document.body).prepend(notice);
      }
      notice.textContent = `${error.message} Showing the last known lease, if any; your command can still run.`;
    }
    if (!lease || (lease.state && lease.state !== "active")) return true;
    return confirmLease(lease, target.id, action);
  }
  document.addEventListener("input", event => {
    if (event.target.matches("#leaseq, #leaselabel")) filter();
  });
  document.addEventListener("click", async event => {
    const button = event.target.closest("[data-force-release]");
    if (!button || button.disabled) return;
    button.disabled = true;
    const id = button.dataset.forceRelease;
    try {
      const lease = await get(`/v1/leases/${encodeURIComponent(id)}`);
      if (!await confirmLease(lease, lease.group, "Force release", true)) return;
      const response = await fetch(`/v1/leases/${encodeURIComponent(id)}/force-release`, {
        method: "POST", headers: {"Content-Type": "application/json", "X-Privy-UI": "1"},
        body: "{}", cache: "no-store"
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error?.message || `Force release failed (${response.status}).`);
      location.reload();
    } catch (error) {
      let notice = $("lease-action-error");
      if (!notice) {
        notice = element("p", "", "lease-action-error");
        notice.id = "lease-action-error"; notice.setAttribute("role", "alert");
        $("s-leases").append(notice);
      }
      notice.textContent = error.message;
    } finally { button.disabled = false; }
  });
  filter();
  return {filter, confirmTarget};
})();
