"""Transient, same-origin Relay SAS generator dashboard."""

SAS_CSS = """
#relay-sas{max-width:950px}
.sas-card{background:var(--paper);border:1px solid var(--line);border-radius:10px;padding:24px;margin-top:22px}
#relay-sas label{display:block;font-weight:600;margin:16px 0 6px}
#relay-sas input,#relay-sas textarea{width:100%;padding:11px 12px;border:1px solid var(--line);
border-radius:6px;background:var(--code);color:var(--ink);font:12px var(--mono)}
#relay-sas textarea{resize:vertical;min-height:110px}
#relay-sas button{padding:9px 16px;border:1px solid var(--line);border-radius:6px;background:var(--paper);
color:var(--ink);cursor:pointer;font:600 12px var(--sans)}
#relay-sas button.primary{background:var(--accent);color:var(--paper);border-color:var(--accent)}
#relay-sas button:disabled{opacity:.55;cursor:wait}
.sas-actions{display:flex;gap:10px;margin-top:20px;align-items:center}
.sas-help{color:var(--muted);font-size:12px;margin:6px 0}
.sas-error{color:var(--red);margin-top:16px}
.sas-expiry{color:var(--accent);font-weight:600;overflow-wrap:anywhere}
"""

SAS_HTML = """
<div id="relay-sas">
<div class="hero"><div class="eyebrow">SHORT-LIVED RELAY CREDENTIALS</div>
<h1>Relay SAS generator</h1>
<p>Paste a Relay connection string to create a time-bound credential for one Hybrid Connection.</p></div>
<form id="sas-form" class="sas-card" autocomplete="off">
<h3>Connection and lifetime</h3>
<label for="sas-connection">Relay connection string</label>
<input id="sas-connection" type="password" required autocomplete="off" spellcheck="false"
placeholder="Endpoint=sb://...;SharedAccessKeyName=...;SharedAccessKey=...;EntityPath=...">
<p class="sas-help">Processed by this local hub only. Not saved to disk, browser storage, or Relay configuration.</p>
<div id="sas-entity-field"><label for="sas-entity">Hybrid Connection name</label>
<input id="sas-entity" required autocomplete="off" spellcheck="false" placeholder="my-hybrid-connection">
<p class="sas-help">Required when the connection string has no EntityPath. Namespace-wide tokens are not generated.</p></div>
<label for="sas-hours">Lifetime (hours)</label>
<input id="sas-hours" type="number" min="0.000278" step="any" value="6" required>
<p class="sas-help">Positive lifetime, rounded to the nearest whole second.</p>
<div class="sas-actions"><button class="primary" id="sas-generate" type="submit">Generate SAS</button>
<button id="sas-reset" type="reset">Clear all</button></div>
<p id="sas-error" class="sas-error" role="alert" hidden></p>
</form>
<section id="sas-output" class="sas-card" hidden aria-label="Generated credentials">
<h3>Ready to copy</h3><p id="sas-expiry" class="sas-expiry"></p>
<p class="sas-help">These are bearer secrets. Anyone holding either value can use the policy's permissions until expiry.</p>
<label for="sas-token">SAS token</label><textarea id="sas-token" readonly spellcheck="false"></textarea>
<button type="button" data-sas-copy="sas-token">Copy SAS token</button>
<label for="sas-url">HTTPS sender URL</label><textarea id="sas-url" readonly spellcheck="false"></textarea>
<button type="button" data-sas-copy="sas-url">Copy sender URL</button>
<p id="sas-copy-status" role="status" class="sas-help"></p>
</section>
<div class="sas-card"><h3>Choose a Send-only policy</h3>
<p class="sas-help">Permissions are inherited from the supplied authorization policy, not restricted by this token.
Use a Send-only policy for sender access. The policy's name does not prove its permissions.
A Listen or Manage key cannot be downgraded by shortening the lifetime.</p>
<p class="sas-help">Expiry prevents later authentication; it does not guarantee termination of already-established connections.
This tool signs locally and does not check the policy or credentials against Azure.</p></div>
</div>
"""

SAS_JS = r"""
(() => {
  const form = document.getElementById("sas-form");
  if (!form) return;
  const el = id => document.getElementById(id);
  let generation = 0, pending = null;
  const clearOutput = () => {
    generation++;
    pending?.abort();
    pending = null;
    el("sas-generate").disabled = false;
    el("sas-output").hidden = true;
    el("sas-token").value = "";
    el("sas-url").value = "";
    el("sas-expiry").textContent = "";
    el("sas-copy-status").textContent = "";
    el("sas-error").textContent = "";
    el("sas-error").hidden = true;
  };
  const entityField = () => {
    const hasEntity = el("sas-connection").value.split(";").some(part =>
      /^\s*EntityPath\s*=/i.test(part));
    el("sas-entity-field").hidden = hasEntity;
    el("sas-entity").required = !hasEntity;
  };
  form.addEventListener("input", () => { clearOutput(); entityField(); });
  form.addEventListener("reset", () => {
    clearOutput();
    setTimeout(entityField, 0);
  });
  form.addEventListener("submit", async event => {
    event.preventDefault();
    clearOutput();
    const revision = generation;
    const ttl = Math.round(Number(el("sas-hours").value) * 3600);
    if (!Number.isSafeInteger(ttl) || ttl <= 0) {
      el("sas-error").textContent = "Choose a positive lifetime of at least one second.";
      el("sas-error").hidden = false;
      return;
    }
    const controller = new AbortController();
    pending = controller;
    el("sas-generate").disabled = true;
    try {
      const response = await fetch("/v1/relay-sas", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Privy-UI": "1" },
        cache: "no-store",
        signal: controller.signal,
        body: JSON.stringify({
          connection_string: el("sas-connection").value,
          entity_path: el("sas-entity-field").hidden ? null : el("sas-entity").value,
          ttl_seconds: ttl
        })
      });
      const result = await response.json();
      if (revision !== generation) return;
      if (!response.ok) throw new Error(result.error?.message || "Could not generate SAS credentials.");
      el("sas-token").value = result.token;
      el("sas-url").value = result.sender_url;
      el("sas-expiry").textContent = `Expires ${result.expires_utc} | ${result.endpoint}`;
      el("sas-connection").value = "";
      entityField();
      el("sas-output").hidden = false;
    } catch (error) {
      if (revision !== generation || error.name === "AbortError") return;
      el("sas-error").textContent = error instanceof TypeError ?
        "Could not reach the hub. Check the connection and try again." : error.message;
      el("sas-error").hidden = false;
    } finally {
      if (revision === generation) {
        pending = null;
        el("sas-generate").disabled = false;
      }
    }
  });
  document.querySelectorAll("[data-sas-copy]").forEach(button => button.addEventListener("click", async () => {
    const revision = generation;
    try {
      await navigator.clipboard.writeText(el(button.dataset.sasCopy).value);
      if (revision === generation) el("sas-copy-status").textContent = "Copied.";
    } catch {
      if (revision === generation)
        el("sas-copy-status").textContent = "Clipboard unavailable. Select the value and copy it manually.";
    }
  }));
  window.addEventListener("pagehide", () => { clearOutput(); form.reset(); });
  entityField();
})();
"""
