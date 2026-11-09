window.PrivyLeases = (() => {
  "use strict";
  let dialog = null;
  let bulkBusy = false;
  const selected = new Set(JSON.parse(sessionStorage.getItem("privy.lease-selection") || "[]"));
  const $ = id => document.getElementById(id);
  const operatorKey = "privy.manual-operator";

  function operator() {
    const saved = localStorage.getItem(operatorKey);
    if (!saved) return null;
    const value = JSON.parse(saved);
    if (!value || typeof value.session_id !== "string") throw new Error("Invalid saved manual operator identity.");
    return value;
  }
  function notice(message) {
    let node = $("lease-action-error");
    if (!node) {
      node = element("p", "", "lease-action-error");
      node.id = "lease-action-error"; node.setAttribute("role", "alert");
      ($("s-leases") || document.body).append(node);
    }
    node.textContent = message;
  }

  function filter() {
    if (!$("leaseq")) return;
    try {
      const owner = operator()?.client_id;
      document.querySelectorAll("[data-owner-release]").forEach(button => {
        button.hidden = button.dataset.client !== owner;
      });
    } catch (error) { notice(`Cannot read manual operator identity: ${error.message}`); }
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
    document.querySelectorAll("[data-select-lease]").forEach(input => {
      if (input.closest("tr").dataset.bulkSafe !== "true") selected.delete(input.dataset.selectLease);
      input.checked = selected.has(input.dataset.selectLease);
    });
    document.querySelectorAll("[data-lease-row]").forEach(row => {
      if (!row.querySelector("[data-select-lease]")) selected.delete(row.dataset.leaseId);
    });
    updateSelection();
    persistSelection();
  }

  function persistSelection() {
    sessionStorage.setItem("privy.lease-selection", JSON.stringify([...selected]));
  }
  function visibleSelections() {
    return [...document.querySelectorAll("[data-select-lease]")].filter(input => !input.closest("tr").hidden);
  }
  function updateSelection() {
    const inputs = visibleSelections(), count = inputs.filter(input => input.checked).length;
    const button = $("force-release-selected"), all = $("select-all-leases");
    if (button) { button.disabled = count === 0 || bulkBusy; button.textContent = `Force release selected (${count})`; }
    if (all) {
      all.checked = inputs.length > 0 && count === inputs.length;
      all.indeterminate = count > 0 && count < inputs.length;
    }
  }
  async function releaseSelected() {
    if (dialog || bulkBusy) return;
    const inputs = visibleSelections().filter(input => input.checked);
    if (!inputs.length) return;
    const selectedIds = inputs.map(input => input.dataset.selectLease);
    const button = $("force-release-selected"); bulkBusy = true; button.disabled = true;
    try {
      const leases = await Promise.all(selectedIds.map(id => get(`/v1/leases/${encodeURIComponent(id)}`)));
      const panel = element("dialog", "", "lease-dialog"); dialog = panel;
      const heading = element("h2", `Force release ${leases.length} selected leases?`);
      heading.id = "lease-bulk-title"; panel.setAttribute("aria-labelledby", heading.id);
      panel.append(heading, element("p",
        "This releases whole groups and skips cooldown. Existing pin reconciliation rules still apply; remote work may still be running. Verify it has stopped before continuing.",
        "lease-warning"));
      const list = element("ul", "");
      const releasable = [];
      const blocked = [];
      for (const lease of leases) {
        const runningJobs = Array.isArray(lease.running_jobs) ? lease.running_jobs : [];
        const owners = lease.job_owners && typeof lease.job_owners === "object" && !Array.isArray(lease.job_owners)
          ? Object.keys(lease.job_owners) : [];
        const running = [...new Set([...runningJobs, ...owners])];
        const invalidMetadata = (lease.running_jobs && !Array.isArray(lease.running_jobs)) ||
          (lease.job_owners && (typeof lease.job_owners !== "object" || Array.isArray(lease.job_owners)));
        const isBlocked = running.length > 0 || invalidMetadata;
        if (isBlocked) {
          blocked.push(lease);
          selected.delete(lease.lease_id);
        } else releasable.push(lease);
        list.append(element("li", `${lease.lease_id} · ${lease.group} · ${lease.holder?.name || lease.client_id}` +
          (isBlocked ? ` — BLOCKED: ${running.length ? `running jobs/pins ${running.join(", ")}` : "invalid pin metadata"}; will not be submitted`
            : " — no running jobs or pins; eligible"),
        isBlocked ? "lease-warning" : ""));
      }
      persistSelection();
      const results = element("div", ""); results.setAttribute("role", "status");
      blocked.forEach(lease => results.append(element("p",
        `${lease.lease_id}: skipped; running jobs or pins were detected, so no release request will be sent.`,
        "lease-warning")));
      const actions = element("div", "", "lease-dialog-actions");
      const cancel = element("button", "Cancel", "lease-button");
      const proceed = element("button", `Force release unpinned (${releasable.length})`, "lease-button danger");
      proceed.disabled = releasable.length === 0;
      cancel.type = proceed.type = "button"; actions.append(cancel, proceed);
      panel.append(list, results, actions); document.body.append(panel);
      let submitting = false, completed = false;
      const close = () => {
        if (submitting) return;
        panel.close(); panel.remove(); dialog = null; bulkBusy = false;
        persistSelection();
        if (completed) location.reload(); else { updateSelection(); button.focus(); }
      };
      cancel.onclick = close;
      panel.addEventListener("cancel", event => { event.preventDefault(); close(); });
      proceed.onclick = async () => {
        if (submitting || completed) return;
        submitting = true; proceed.disabled = true; cancel.disabled = true;
        results.replaceChildren();
        let freshLeases;
        try {
          freshLeases = await Promise.all(selectedIds.map(id => get(`/v1/leases/${encodeURIComponent(id)}`)));
        } catch (error) {
          results.append(element("p",
            `Could not refresh selected leases before submission: ${error.message}. No releases were submitted.`,
            "lease-action-error"));
          submitting = false; completed = true; cancel.disabled = false;
          cancel.textContent = "Close and refresh";
          return;
        }
        list.replaceChildren();
        for (const lease of freshLeases) {
          const runningJobs = Array.isArray(lease.running_jobs) ? lease.running_jobs : [];
          const owners = lease.job_owners && typeof lease.job_owners === "object" && !Array.isArray(lease.job_owners)
            ? Object.keys(lease.job_owners) : [];
          const running = [...new Set([...runningJobs, ...owners])];
          const invalidMetadata = (lease.running_jobs && !Array.isArray(lease.running_jobs)) ||
            (lease.job_owners && (typeof lease.job_owners !== "object" || Array.isArray(lease.job_owners)));
          const isBlocked = running.length > 0 || invalidMetadata;
          list.append(element("li", `${lease.lease_id} · ${lease.group} · ${lease.holder?.name || lease.client_id}` +
            (isBlocked ? ` — BLOCKED: ${running.length ? `running jobs/pins ${running.join(", ")}` : "invalid pin metadata"}; will not be submitted`
              : " — no running jobs or pins; eligible"),
          isBlocked ? "lease-warning" : ""));
          if (isBlocked) {
            selected.delete(lease.lease_id);
            persistSelection();
            results.append(element("p",
              `${lease.lease_id}: skipped; running jobs or pins were detected during pre-submit refresh.`,
              "lease-warning"));
            continue;
          }
          const result = element("p", `${lease.lease_id}: releasing…`); results.append(result);
          selected.delete(lease.lease_id);
          persistSelection();
          try {
            await mutate(`/v1/leases/${encodeURIComponent(lease.lease_id)}/force-release`, "POST", {
              require_unpinned: true
            });
            result.textContent = `${lease.lease_id}: released`;
          } catch (error) {
            result.textContent = `${lease.lease_id}: ${error.name === "AbortError"
              ? "Timed out; outcome unknown. Check the refreshed list before retrying." : error.message}`;
            result.className = "lease-action-error";
          }
        }
        submitting = false; completed = true; cancel.disabled = false;
        cancel.textContent = "Close and refresh";
      };
      panel.showModal(); cancel.focus();
    } catch (error) { bulkBusy = false; notice(error.message); updateSelection(); }
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
  async function mutate(path, method, body) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 30000);
    try {
      const response = await fetch(path, {
        method, headers: {"Content-Type": "application/json", "X-Privy-UI": "1"},
        ...(body === undefined ? {} : {body: JSON.stringify(body)}),
        cache: "no-store", signal: controller.signal
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error?.message || `Lease request failed (${response.status}).`);
      return result;
    } finally { clearTimeout(timer); }
  }
  async function takeLease(button) {
    if (dialog) return;
    const previous = document.activeElement;
    const panel = element("dialog", "", "lease-dialog");
    panel.id = "lease-create"; panel.setAttribute("aria-labelledby", "lease-create-title");
    dialog = panel;
    const heading = element("h2", "Take a lease"); heading.id = "lease-create-title";
    const form = element("form", "", "lease-form");
    function field(title, id, tag = "input") {
      const label = element("label", title), input = element(tag, "");
      input.id = id; label.append(input); form.append(label);
      return input;
    }
    const group = field("Devbox / lease group", "lease-group", "select");
    group.required = true;
    const loading = element("option", "Loading groups..."); loading.value = ""; group.append(loading);
    const members = element("p", ""); form.append(members);
    const holder = field("Holder name", "lease-holder");
    holder.required = true; holder.maxLength = 240; holder.value = "Manual reservation";
    const purpose = field("Purpose", "lease-purpose");
    purpose.required = true; purpose.maxLength = 240;
    purpose.placeholder = "Reserved for my work";
    const repo = field("Repository label (optional)", "lease-repo"); repo.maxLength = 1024;
    const duration = element("div", "", "duration-picker");
    const maximum = Number(button.dataset.maxTtl), initial = Math.max(1, Math.ceil(Number(button.dataset.defaultTtl) / 60));
    const timeLabel = minutes => `${String(Math.floor(minutes / 60)).padStart(2, "0")}:${String(minutes % 60).padStart(2, "0")}`;
    const inputs = {};
    for (const [part, label] of [["hours", "Hours"], ["minutes", "Minutes"]]) {
      const wrap = element("label", label), input = element("input", "");
      input.type = "number"; input.id = `lease-${part}`; input.min = "0"; input.step = "1"; input.required = true;
      input.max = part === "hours" ? String(Math.floor(maximum / 3600)) : "59";
      input.value = String(part === "hours" ? Math.floor(initial / 60) : initial % 60);
      inputs[part] = input;
      wrap.append(input); duration.append(wrap);
      for (const [sign, delta] of [["Decrease", -1], ["Increase", 1]]) {
        const step = element("button", delta > 0 ? "+" : "-", "lease-button");
        step.type = "button"; step.setAttribute("aria-label", `${sign} ${part}`);
        step.onclick = () => {
          const minutes = Number(inputs.hours.value) * 60 + Number(inputs.minutes.value) + delta * (part === "hours" ? 60 : 1);
          if (!Number.isInteger(minutes) || minutes < 1 || minutes * 60 > maximum) return;
          inputs.hours.value = String(Math.floor(minutes / 60)); inputs.minutes.value = String(minutes % 60);
          updateDuration();
        };
        duration.append(step);
      }
    }
    const display = element("output", ""); display.id = "lease-duration"; duration.append(display);
    const expiry = element("p", "");
    form.append(duration, expiry, element("p",
      `Duration HH:MM, up to ${timeLabel(Math.floor(maximum / 60))}. Reserves the whole group. ` +
      "Advisory: unleased commands can still run. Closing this page does not release or renew the lease."));
    const error = element("p", "", "lease-action-error"); error.id = "lease-create-error"; error.setAttribute("role", "alert");
    const actions = element("div", "", "lease-dialog-actions");
    const cancel = element("button", "Cancel", "lease-button"); cancel.type = "button";
    const submit = element("button", "Take lease", "lease-button primary"); submit.type = "submit"; submit.disabled = true;
    actions.append(cancel, submit); form.append(error, actions); panel.append(heading, form);
    let submitting = false;
    const close = () => {
      if (submitting) return;
      panel.close(); panel.remove(); dialog = null;
      if (previous?.isConnected) previous.focus();
    };
    cancel.onclick = close;
    panel.addEventListener("cancel", event => { event.preventDefault(); close(); });
    function updateDuration() {
      const hours = inputs.hours.valueAsNumber, minutes = inputs.minutes.valueAsNumber;
      const total = hours * 60 + minutes;
      const valid = Number.isInteger(hours) && Number.isInteger(minutes) && hours >= 0 &&
        minutes >= 0 && minutes < 60 && total >= 1 && total * 60 <= maximum;
      inputs.minutes.setCustomValidity(valid ? "" : `Choose a duration between 00:01 and ${timeLabel(Math.floor(maximum / 60))}.`);
      display.textContent = valid ? timeLabel(total) : "--:--";
      expiry.textContent = valid ? `Expires around ${new Date(Date.now() + total * 60000).toLocaleString()} (local time).` : "Enter a valid duration.";
      return valid ? total * 60 : null;
    }
    form.addEventListener("input", updateDuration);
    form.onsubmit = async event => {
      event.preventDefault();
      const ttl = updateDuration();
      if (submitting || !form.reportValidity() || ttl === null) return;
      submitting = true; submit.disabled = true; cancel.disabled = true; error.textContent = "";
      try {
        let identity = operator() || {session_id: crypto.randomUUID()};
        localStorage.setItem(operatorKey, JSON.stringify(identity));
        const client = await mutate("/v1/clients", "POST", {
          name: holder.value.trim(), kind: "human-ui", session_id: identity.session_id
        });
        identity = {...identity, client_id: client.client_id, name: client.name};
        localStorage.setItem(operatorKey, JSON.stringify(identity));
        const lease = await mutate("/v1/leases", "POST", {
          client_id: client.client_id, target: group.value, purpose: purpose.value.trim(),
          labels: repo.value.trim() ? {repo: repo.value.trim()} : {}, ttl
        });
        location.assign(`/ui/leases?state=active&peek=${encodeURIComponent(lease.lease_id)}#s-lease-peek`);
      } catch (failure) {
        error.textContent = failure.name === "AbortError"
          ? "The request timed out. Check the lease list before retrying; the reservation may have succeeded."
          : failure.message;
      } finally { submitting = false; submit.disabled = false; cancel.disabled = false; }
    };
    document.body.append(panel); panel.showModal(); updateDuration();
    try {
      const identity = operator();
      if (identity?.name) { holder.value = identity.name; holder.readOnly = true; }
      const groups = await get("/v1/groups");
      if (!panel.isConnected) return;
      group.replaceChildren();
      const placeholder = element("option", "Choose a devbox"); placeholder.value = ""; group.append(placeholder);
      for (const item of groups) {
        const option = element("option", `${item.group} (${item.state})`);
        option.value = item.group; option.disabled = item.state !== "free";
        group.append(option);
      }
      group.onchange = () => {
        const selected = groups.find(item => item.group === group.value);
        members.textContent = selected ? `Members: ${Object.values(selected.members).join(", ")}` : "";
      };
      submit.disabled = !groups.some(item => item.state === "free");
      if (submit.disabled) error.textContent = "No group is available. Existing reservations are never overridden here.";
      group.focus();
    } catch (failure) { error.textContent = failure.message; }
  }
  function confirmLease(lease, target, action, force = false) {
    if (dialog) return Promise.resolve(false);
    const previous = document.activeElement;
    const panel = element("dialog", "", "lease-dialog");
    dialog = panel;
    panel.setAttribute("aria-labelledby", "lease-dialog-title");
    const own = action === "Release";
    panel.append(element("div", force ? "DASHBOARD ONLY" : own ? "YOUR RESERVATION" : "HEADS UP", "eyebrow"));
    const heading = element("h2", force ? `Force release ${lease.lease_id}?` : own ? `Release ${target}?` : `${target} is leased by another session`);
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
      ? "This releases the whole group and skips cooldown. Reconciliation may free pins only when their hub owner is stopped or unresolved; remote work may still be running. Verify it has stopped before continuing."
      : own
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
    const proceed = element("button", force ? "Force release" : own ? "Release" : `${action} anyway`, `lease-button ${force ? "danger" : "primary"}`);
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
      const data = await get("/v1/targets?probe=false");
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
  document.addEventListener("change", event => {
    if (event.target.matches("#select-all-leases")) {
      visibleSelections().forEach(input => { input.checked = event.target.checked; });
    }
    if (event.target.matches("#select-all-leases, [data-select-lease]")) {
      document.querySelectorAll("[data-select-lease]").forEach(input => {
        if (input.checked) selected.add(input.dataset.selectLease); else selected.delete(input.dataset.selectLease);
      });
      persistSelection();
      updateSelection();
    }
  });
  document.addEventListener("click", async event => {
    const quick = event.target.closest("#select-never-used, #select-idle");
    if (quick) {
      const cutoff = Date.now() / 1000 - Number($("lease-idle-threshold").value);
      visibleSelections().forEach(input => {
        const row = input.closest("tr"), last = Number(row.dataset.lastUsed);
        input.checked = row.dataset.bulkSafe === "true" && row.dataset.useKnown === "true" &&
          (quick.id === "select-never-used" ? last === 0
          : (last || Number(row.dataset.acquired)) < cutoff);
        if (input.checked) selected.add(input.dataset.selectLease); else selected.delete(input.dataset.selectLease);
      });
      persistSelection();
      updateSelection(); return;
    }
    if (event.target.closest("#force-release-selected")) { await releaseSelected(); return; }
    const take = event.target.closest("#take-lease");
    if (take) { await takeLease(take); return; }
    const button = event.target.closest("[data-force-release], [data-owner-release]");
    if (!button || button.disabled) return;
    button.disabled = true;
    const force = Boolean(button.dataset.forceRelease);
    const id = button.dataset.forceRelease || button.dataset.ownerRelease;
    try {
      const lease = await get(`/v1/leases/${encodeURIComponent(id)}`);
      const owner = force ? null : operator()?.client_id;
      if (!force && owner !== lease.client_id) throw new Error("This browser does not own the reservation. Use the separately confirmed Force release action if needed.");
      if (!await confirmLease(lease, lease.group, force ? "Force release" : "Release", force)) return;
      await mutate(force ? `/v1/leases/${encodeURIComponent(id)}/force-release`
        : `/v1/leases/${encodeURIComponent(id)}?client_id=${encodeURIComponent(owner)}`, force ? "POST" : "DELETE", force ? {} : undefined);
      location.reload();
    } catch (error) {
      notice(error.message);
    } finally { button.disabled = false; }
  });
  filter();
  return {filter, confirmTarget};
})();
