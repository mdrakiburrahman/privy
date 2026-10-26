"""Self-contained, stateless command workspace over the existing hub job API."""

from __future__ import annotations

import html
import json
from typing import Any

TERMINAL_ICON = (
    '<svg class="terminal-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    'stroke-width="1.7" aria-hidden="true"><rect x="3" y="4" width="18" height="16" rx="4"/>'
    '<path d="m7 9 3 3-3 3m6 0h4"/></svg>'
)


def execution_choices(info: dict[str, Any]) -> list[str]:
    """Only advertise confirmed subprocess runtimes; notebook Python is not a fresh process."""
    languages = (info.get("capabilities") or {}).get("languages") or []
    runtimes = info.get("runtimes") or {}
    return [name for name in ("powershell", "bash", "python") if name in languages and runtimes.get(name)]


def workspace(service: Any, report: dict[str, Any], selected: str) -> str:
    targets = []
    for row in report["targets"]:
        target = service.registry.resolve(row["id"])
        probe = service.registry.cached(target)
        info = probe.info if probe and probe.info else {}
        choices = execution_choices(info)
        system = (info.get("platform") or {}).get("system")
        preferred = "powershell" if system == "Windows" else "bash"
        history = []
        for job in service.jobs.list(target_id=target.id, limit=20):
            if job.type != "exec" or job.spec["mode"] != "subprocess":
                continue
            history.append(
                {
                    **job.summary(include_created=True),
                    "code": job.spec["code"] if len(job.spec["code"]) <= 65536 else None,
                    "preview": job.spec["code"][:100],
                    "timeout_s": job.spec["timeout_s"],
                }
            )
        targets.append(
            {
                "id": row["id"],
                "label": row["label"],
                "status": row["status"],
                "host": row["host"],
                "os": row["os"],
                "languages": choices,
                "default": preferred if preferred in choices else next(iter(choices), ""),
                "history": history,
            }
        )
    data = html.escape(json.dumps(targets, ensure_ascii=True), quote=True)
    selected_attr = html.escape(selected, quote=True)
    return f"""
<section id="remote-exec" data-target="{selected_attr}">
<div id="exec-data" hidden data-targets="{data}"></div>
<div class="exec-heading"><div><div class="eyebrow">YOUR FLEET, AT YOUR FINGERTIPS</div>
<h1>{TERMINAL_ICON}Remote Exec</h1>
<p>One machine. Any supported language. A little less distance.</p></div>
<span class="exec-badge"><i></i> RELAY CONNECTED WORKSPACE</span></div>
<noscript><p class="status warn">JavaScript is required to compose and run commands. The existing REST API remains available.</p></noscript>
<div class="exec-controls">
<label class="exec-field">Find a machine<input id="exec-search" type="search"
 placeholder="Search name, host, platform..." autocomplete="off"></label>
<label class="exec-field exec-machine">Online HYCO<select id="exec-target" aria-describedby="exec-machine-note"></select></label>
<label class="exec-field">Language<select id="exec-language"></select></label>
<label class="exec-field exec-timeout">Timeout (seconds)<input id="exec-timeout" type="number" min="1"
 max="86400" value="600" step="1"></label>
<button type="button" id="exec-refresh" class="exec-button">Refresh targets</button>
</div>
<p id="exec-machine-note" class="fine"></p>
<div id="exec-message" role="alert" hidden></div>
<div class="exec-grid">
<div class="exec-session">
<div class="exec-panel exec-editor-panel">
<div class="exec-panel-head"><span><i class="exec-led"></i> <b>Script</b>
 <span id="exec-file" class="fine"></span></span><span class="exec-key">Ctrl / Cmd + Enter</span></div>
<div class="exec-editor-wrap">
<textarea id="exec-code" spellcheck="false" autocapitalize="off" autocomplete="off"
 aria-label="Remote script" aria-describedby="exec-edit-help" aria-controls="exec-suggestions"
 placeholder="Choose a machine and write something useful..." wrap="off"></textarea>
<div id="exec-suggestions" role="listbox" aria-label="Local suggestions" hidden></div>
</div>
<div class="exec-editor-foot"><span id="exec-edit-help">Tab: indent / accept suggestion · Ctrl+Space: suggestions</span>
<span id="exec-position">Ln 1, Col 1</span></div>
<div class="exec-actions"><span class="fine">Fresh process every run. No interactive input.</span>
<button type="button" id="exec-run" class="exec-button primary" disabled>Run script <span aria-hidden="true">↗</span></button></div>
</div>
<div class="exec-panel exec-output-panel">
<div class="exec-panel-head"><span><b>Output</b> <span id="exec-state" class="exec-state" role="status">Ready</span></span>
<div class="exec-output-actions"><a id="exec-log" hidden>Full log</a>
<button id="exec-reconnect" type="button" class="exec-button" hidden>Reconnect</button>
<button id="exec-cancel" type="button" class="exec-button" disabled>Cancel job</button></div></div>
<div id="exec-job" class="exec-job mono">Your next command starts here.</div>
<div class="exec-output"><div class="exec-stream-label">STDOUT</div>
<pre id="exec-stdout" tabindex="0" aria-label="Standard output"></pre>
<div class="exec-stream-label stderr">STDERR</div>
<pre id="exec-stderr" tabindex="0" aria-label="Standard error"></pre></div>
<div class="exec-output-foot fine">Output follows automatically when scrolled to the bottom.
 Leaving this page does not cancel a job.</div>
</div></div>
<aside class="exec-panel exec-history"><div class="exec-panel-head"><b>Recent runs</b><span class="fine">THIS HYCO</span></div>
<div id="exec-history"></div><div class="exec-history-note fine">Load a script to edit and run again.
 Opening output never executes a command. Executed scripts are recorded in the hub audit log.</div></aside>
</div>
<div class="exec-bottom"><span>{TERMINAL_ICON} No listener upgrades. No extra agents.</span>
<span>Local suggestions, not remote IntelliSense. Include <code>cd</code> and variables in the same script.</span></div>
</section>
"""


EXEC_CSS = """
.terminal-icon{width:18px;height:18px;vertical-align:middle;flex-shrink:0}
.sidebar nav a:has(.terminal-icon){justify-content:flex-start;gap:9px}
.remote-link{display:inline-flex;align-items:center;gap:4px;border:1px solid var(--line);border-radius:5px;
padding:2px 6px;margin-top:5px;font-size:10px;background:var(--accent-soft)}
.remote-link .terminal-icon{width:13px;height:13px}
#remote-exec{max-width:1800px;margin:auto}
.exec-heading{display:flex;justify-content:space-between;align-items:center;gap:20px;margin:6px 0 28px}
.exec-heading h1{font-size:36px;letter-spacing:-1.4px;line-height:1.3;margin:9px 0 8px;font-weight:620}
.exec-heading h1 svg{width:34px;height:34px;margin-right:11px;color:var(--accent)}
.exec-heading p{color:var(--muted);font-size:12px}
.exec-badge{font:9px var(--mono);letter-spacing:1px;color:var(--muted);white-space:nowrap}
.exec-badge i,.exec-led{display:inline-block;width:6px;height:6px;border-radius:50%;background:var(--accent);margin-right:6px}
.exec-controls{display:flex;align-items:end;flex-wrap:wrap;gap:12px;padding:18px;background:var(--paper);
border:1px solid var(--line);border-radius:12px;box-shadow:0 4px 14px #102b3104}
.exec-field{display:grid;gap:7px;font-size:10px;font-weight:600;color:var(--muted);flex:1;min-width:130px}
.exec-machine{flex:1.6;min-width:200px}.exec-timeout{flex:0 1 105px;min-width:100px}
.exec-field input,.exec-field select{width:100%;height:37px;background:var(--code);border:1px solid var(--line);
border-radius:7px;padding:7px 10px;color:var(--ink);font:11px var(--mono);min-width:0}
.exec-field select{max-width:100%;text-overflow:ellipsis}
.exec-button{border:1px solid var(--line);border-radius:7px;padding:9px 12px;background:var(--paper);color:var(--ink);
font-family:inherit;font-weight:600;font-size:11px;cursor:pointer;white-space:nowrap}
.exec-button:hover:not(:disabled){background:var(--accent-soft);border-color:var(--accent)}
.exec-button:disabled{opacity:.45;cursor:not-allowed}
.exec-button.primary{background:var(--accent);color:var(--paper);border-color:var(--accent);padding:10px 20px}
.exec-button.primary:hover:not(:disabled){background:var(--accent);filter:brightness(1.1)}
#remote-exec :focus-visible,.remote-link:focus-visible{outline:2px solid var(--accent);outline-offset:3px}
#exec-machine-note{margin:10px 4px 18px;min-height:17px}
#exec-message{padding:12px 16px;border:1px solid var(--red);background:var(--red-soft);color:var(--red);
border-radius:8px;margin-bottom:16px;white-space:pre-wrap;overflow-wrap:anywhere}
.exec-grid{display:grid;grid-template-columns:minmax(0,1fr) 270px;gap:18px}
.exec-session{display:grid;gap:18px;min-width:0}
.exec-panel{border:1px solid var(--line);background:var(--paper);border-radius:12px;overflow:hidden;
box-shadow:0 4px 18px #102b3104}
.exec-panel-head{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:13px 17px;
border-bottom:1px solid var(--line);font-size:11px;min-height:49px}
.exec-panel-head b{font-weight:650}.exec-key{font:9px var(--mono);color:var(--faint)}
.exec-editor-wrap{position:relative;background:var(--code)}
#exec-code{display:block;resize:vertical;width:100%;min-height:260px;border:0;padding:22px;
font:12px/1.85 var(--mono);background:transparent;color:var(--ink);tab-size:2;outline-offset:-3px!important}
.exec-editor-foot{display:flex;justify-content:space-between;gap:10px;background:var(--code);color:var(--faint);
padding:8px 17px;font:9px var(--mono);border-top:1px solid var(--line)}
.exec-actions{display:flex;justify-content:space-between;align-items:center;gap:12px;padding:12px 17px;
border-top:1px solid var(--line)}
.exec-state{margin-left:10px;padding:3px 7px;background:var(--accent-soft);color:var(--accent);border-radius:4px;font:9px var(--mono)}
.exec-state[data-state=running],.exec-state[data-state=queued]{background:var(--orange-soft);color:var(--orange)}
.exec-state[data-state=failed],.exec-state[data-state=error],.exec-state[data-state=timed_out]{background:var(--red-soft);color:var(--red)}
.exec-output-actions{display:flex;align-items:center;gap:10px;font-size:10px}
.exec-output-actions button{padding:5px 8px;font-size:10px}
.exec-job{padding:11px 17px;font-size:9px;color:var(--muted);border-bottom:1px solid var(--line);overflow-wrap:anywhere}
.exec-output{background:#102127;color:#d3e7df;padding:16px 19px}
.exec-stream-label{font:8px var(--mono);letter-spacing:1.5px;color:#7ba297}
.exec-stream-label.stderr{color:#ef9b9b;margin-top:12px}
.exec-output pre{font:11px/1.8 var(--mono);white-space:pre-wrap;overflow-wrap:anywhere;overflow:auto;
min-height:42px;max-height:290px;margin:8px 0;color:#d3e7df}
#exec-stderr{color:#ffb9b9}.exec-output-foot{padding:10px 17px}
.exec-history{align-self:start;min-width:0}
.exec-history-item{padding:14px 16px;border-bottom:1px solid var(--line)}
.exec-history-item code{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:10px;margin:9px 0}
.exec-history-item small{font:9px var(--mono);color:var(--muted)}
.exec-history-actions{display:flex;gap:14px;margin-top:9px}
.exec-history-actions button{background:none;border:0;padding:0;color:var(--accent);cursor:pointer;font-family:inherit;font-size:10px}
.exec-history-actions button:disabled{color:var(--faint);cursor:not-allowed}
.exec-history-note{padding:16px;font-size:10px;line-height:1.8}
.exec-bottom{display:flex;justify-content:space-between;flex-wrap:wrap;gap:12px;margin:23px 0;color:var(--faint);font-size:10px}
.exec-bottom svg{width:14px;height:14px;margin-right:5px}
#exec-suggestions{position:absolute;left:18px;right:18px;bottom:8px;max-height:160px;overflow:auto;z-index:2;
border:1px solid var(--line);border-radius:8px;background:var(--paper);box-shadow:0 8px 30px #0002;padding:4px}
#exec-suggestions button{display:block;text-align:left;width:100%;border:0;padding:8px 10px;background:none;
font:11px var(--mono);color:var(--ink);cursor:pointer;border-radius:5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#exec-suggestions button[aria-selected=true]{background:var(--accent-soft);color:var(--accent)}
@media(max-width:1150px){.exec-grid{grid-template-columns:minmax(0,1fr)}.exec-history{max-height:340px;overflow:auto}
.exec-badge{display:none}}
@media(max-width:600px){.exec-heading h1{font-size:29px}.exec-controls{padding:13px}.exec-field{min-width:100%}
.exec-timeout{min-width:110px}.exec-actions,.exec-editor-foot{flex-wrap:wrap}.exec-panel-head{flex-wrap:wrap}
.exec-output-actions{gap:6px}.exec-key{display:none}.topbar:has(+.main #remote-exec){height:auto;min-height:58px;flex-wrap:wrap;padding-block:10px}}
"""


EXEC_JS = r"""
;(() => {
  const root = document.getElementById("remote-exec");
  if (!root) return;
  const $ = id => document.getElementById("exec-" + id);
  let targets = JSON.parse($("data").dataset.targets), current = null, refreshing = false;
  let activeJob = null, submitting = false, watching = false, pollController = null;
  let previousKey = "", suggestions = [], suggestionIndex = 0, suggestionStart = 0;
  const drafts = new Map(), outputLimit = 65536;
  const snippets = {
    powershell: ["Get-Date", "Get-Location", "Get-ChildItem", "Get-Process", "Get-Service", "Get-ComputerInfo",
      "Get-Content -Path ", "Write-Output ", "Get-ChildItem | Where-Object { $_.Name -like '*' }"],
    bash: ["pwd", "ls -lah", "uname -a", "df -h", "ps aux", "cat ", "printf '%s\\n' ", "find . -maxdepth 2 -type f"],
    python: ["print(", "import os", "import sys", "import platform", "print(platform.platform())",
      "from pathlib import Path", "for item in range(10):\n    print(item)"]
  };
  function message(text = "") { $("message").textContent = text; $("message").hidden = !text; }
  async function api(url, options = {}) {
    const response = await fetch(url, { ...options, headers: { "X-Privy-UI": "1", ...options.headers } });
    const body = await response.json();
    if (!response.ok) throw new Error(body.error?.message || body.error || "Request failed: HTTP " + response.status);
    return body;
  }
  function saveDraft() { if (previousKey) drafts.set(previousKey, $("code").value); }
  function position() {
    const before = $("code").value.slice(0, $("code").selectionStart).split("\n");
    $("position").textContent = "Ln " + before.length + ", Col " + (before.at(-1).length + 1);
  }
  function hideSuggestions() {
    suggestions = []; $("suggestions").hidden = true; $("code").removeAttribute("aria-activedescendant");
  }
  function updateRun() {
    $("run").disabled = submitting || !!activeJob || !current || current.status !== "online" ||
      !$("language").value || !$("code").value.trim() || !$("timeout").checkValidity();
    for (const id of ["target", "language", "search"]) $(id).disabled = submitting || !!activeJob;
  }
  function setLanguage() {
    saveDraft();
    previousKey = current ? current.id + ":" + $("language").value : "";
    $("code").value = drafts.get(previousKey) || "";
    const lang = $("language").value;
    $("file").textContent = lang ? "script." + ({ powershell: "ps1", bash: "sh", python: "py" })[lang] : "";
    hideSuggestions(); position(); updateRun();
  }
  function history() {
    const box = $("history"); box.replaceChildren();
    const jobs = current?.history || [];
    if (!jobs.length) {
      const empty = document.createElement("p"); empty.className = "empty"; empty.textContent = "A clean slate. Run your first script.";
      box.append(empty);
    }
    for (const job of jobs) {
      const item = document.createElement("div"); item.className = "exec-history-item";
      const state = document.createElement("span"); state.className = "exec-state"; state.dataset.state = job.state;
      state.textContent = job.state + (job.exit_code !== undefined ? " · exit " + job.exit_code : "");
      const code = document.createElement("code"); code.textContent = job.preview || job.language; code.title = job.preview || "";
      const time = document.createElement("small"); time.textContent = job.created_at || job.job_id;
      const actions = document.createElement("div"); actions.className = "exec-history-actions";
      const load = document.createElement("button"); load.type = "button"; load.textContent = "Load script";
      load.disabled = job.code === null || !current.languages.includes(job.language) || submitting || !!activeJob;
      load.title = job.code === null ? "Script too large to load here; open the full log." : "Edit first, then Run script to rerun";
      load.onclick = () => {
        $("language").value = job.language; setLanguage(); $("code").value = job.code;
        $("timeout").value = job.timeout_s; saveDraft(); position(); updateRun(); $("code").focus();
      };
      const open = document.createElement("button"); open.type = "button"; open.textContent = "Open output";
      open.disabled = submitting || !!activeJob;
      open.onclick = () => { message(); attach(job.job_id, current.id); };
      actions.append(load, open); item.append(state, code, time, actions); box.append(item);
    }
  }
  function selectTarget(id, preserveLanguage = false) {
    saveDraft();
    current = targets.find(t => t.id === id) || null;
    const oldLanguage = $("language").value;
    $("language").replaceChildren();
    for (const lang of current?.languages || []) {
      $("language").add(new Option(({ powershell: "PowerShell", bash: "Bash", python: "Python" })[lang], lang));
    }
    if (!$("language").options.length) $("language").add(new Option("No verified runtime", ""));
    $("language").value = preserveLanguage && current?.languages.includes(oldLanguage) ? oldLanguage : current?.default || "";
    $("machine-note").textContent = !current ? "No matching machine. Refresh or change the search." :
      [current.id, current.host, current.os, current.status].filter(Boolean).join(" · ") +
      (!current.languages.length ? " · Subprocess runtime information unavailable. Refresh capabilities; notebook-only Python is not offered." : "");
    setLanguage(); history();
    const url = new URL(location.href);
    if (current) url.searchParams.set("target", current.id); else url.searchParams.delete("target");
    window.history.replaceState(null, "", url);
  }
  function populate(preferred, preserveLanguage = false) {
    const terms = $("search").value.toLowerCase().split(/\s+/).filter(Boolean);
    const matches = targets.filter(t => terms.every(term => [t.id, t.host, t.os].join(" ").toLowerCase().includes(term)));
    const chosen = targets.find(t => t.id === preferred);
    $("target").replaceChildren();
    if (chosen && !matches.includes(chosen)) matches.unshift(chosen);
    for (const target of matches) {
      const option = new Option(target.label + (target.status === "online" ? "" : " · " + target.status), target.id);
      option.disabled = target.status !== "online";
      $("target").add(option);
    }
    if (!matches.length) $("target").add(new Option("No machines found", ""));
    const wanted = chosen || (!preferred && matches.find(t => t.status === "online"));
    $("target").value = wanted?.id || "";
    selectTarget($("target").value, preserveLanguage);
  }
  async function refresh(force = false) {
    if (refreshing || submitting) return;
    refreshing = true; $("refresh").disabled = true;
    try {
      if (force) await api("/v1/targets?refresh=true");
      const response = await fetch(location.href, { headers: { "X-Privy-UI": "1" } });
      if (!response.ok) throw new Error("Could not refresh workspace: HTTP " + response.status);
      const markup = (await response.text()).replace(/<(style|script)\b[\s\S]*?<\/\1>/gi, "");
      const doc = new DOMParser().parseFromString(markup, "text/html");
      const data = doc.getElementById("exec-data");
      if (!data) throw new Error("Workspace data missing; reload the page.");
      const next = JSON.parse(data.dataset.targets);
      for (const id of ["nav", "topchips", "appinfo"]) {
        const old = document.getElementById(id), fresh = doc.getElementById(id);
        if (old && fresh) old.replaceChildren(...fresh.childNodes);
      }
      const id = current?.id || root.dataset.target;
      targets = next;
      if (!activeJob) populate(id, true);
    } catch (error) { message("Target refresh failed: " + error.message); }
    finally { refreshing = false; $("refresh").disabled = false; }
  }
  function append(stream, text, skipped) {
    const pre = $(stream), stick = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 50;
    const value = pre.textContent + (skipped ? "\n[Earlier output no longer retained: " + skipped + " bytes]\n" : "") + text;
    pre.textContent = value.length > outputLimit ? "[Showing the latest output; open Full log for retained output]\n" + value.slice(-outputLimit) : value;
    if (stick) pre.scrollTop = pre.scrollHeight;
  }
  async function follow() {
    if (!activeJob || watching) return;
    const job = activeJob;
    pollController = new AbortController(); watching = true;
    $("reconnect").hidden = true; $("cancel").disabled = false; updateRun(); history();
    try {
      for (;;) {
        const params = new URLSearchParams({ wait_s: "1", max_output_bytes: "65536",
          stdout_offset: String(job.stdout), stderr_offset: String(job.stderr) });
        const view = await api("/v1/jobs/" + encodeURIComponent(job.id) + "?" + params,
          { signal: pollController.signal });
        for (const stream of ["stdout", "stderr"]) {
          append(stream, view[stream] || "", view[stream + "_skipped_bytes"]);
          job[stream] = view[stream + "_offset"];
        }
        $("state").textContent = view.state + (view.exit_code !== undefined ? " · exit " + view.exit_code : "");
        $("state").dataset.state = view.state;
        if (view.error) message(view.error);
        if (view.done) { activeJob = null; break; }
        await new Promise(resolve => setTimeout(resolve, 300));
      }
    } catch (error) {
      if (error.name !== "AbortError") {
        message("Output connection interrupted: " + error.message + ". The job may still be running. Reconnect; do not resubmit.");
        $("state").textContent = "Disconnected"; $("reconnect").hidden = false;
      }
    } finally {
      watching = false; $("cancel").disabled = !activeJob; updateRun(); history(); await refresh();
    }
  }
  function attach(id, target) {
    activeJob = { id, target, stdout: 0, stderr: 0 };
    $("state").textContent = "Connecting"; $("state").dataset.state = "queued";
    $("stdout").textContent = ""; $("stderr").textContent = "";
    $("job").textContent = target + " / " + id;
    $("log").href = "/ui/jobs/" + encodeURIComponent(id) + "/log"; $("log").hidden = false;
    const url = new URL(location.href); url.searchParams.set("job", id); window.history.replaceState(null, "", url);
    follow();
  }
  async function run() {
    if ($("run").disabled) return;
    const target = current.id, language = $("language").value, code = $("code").value;
    const timeout = Number($("timeout").value);
    submitting = true; updateRun(); message(); hideSuggestions(); history();
    $("state").textContent = "Checking target"; $("state").dataset.state = "queued";
    let requestSent = false;
    try {
      const live = await api("/v1/targets/" + encodeURIComponent(target) + "?refresh=true");
      const info = live.listener_info || {};
      if (live.id !== target || live.status !== "online" || !(info.capabilities?.languages || []).includes(language) ||
          !info.runtimes?.[language]) throw new Error("This target or runtime is no longer available. Refresh targets.");
      requestSent = true;
      const job = await api("/v1/jobs", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ target, language, code, mode: "subprocess", timeout_s: timeout, wait_s: 0,
          max_output_bytes: 0 }) });
      submitting = false; attach(job.job_id, target);
    } catch (error) {
      $("state").textContent = "Submission failed"; $("state").dataset.state = "error";
      message(error.message + (requestSent ? " Submission was not retried. Check Recent runs before submitting again." : ""));
    } finally { submitting = false; updateRun(); await refresh(); }
  }
  async function cancel() {
    if (!activeJob) return;
    $("cancel").disabled = true;
    try {
      const job = await api("/v1/jobs/" + encodeURIComponent(activeJob.id) + "/cancel", { method: "POST" });
      $("state").textContent = job.state;
      if (!watching) follow();
    } catch (error) { message("Cancellation failed: " + error.message); }
    finally { $("cancel").disabled = !activeJob; }
  }
  function showSuggestions(explicit = false) {
    const editor = $("code"), before = editor.value.slice(0, editor.selectionStart);
    const prefix = before.split("\n").at(-1).trimStart();
    if (!prefix && !explicit) { hideSuggestions(); return; }
    suggestionStart = editor.selectionStart - prefix.length;
    const candidates = [...(snippets[$("language").value] || []),
      ...(current?.history || []).filter(j => j.language === $("language").value && j.code).map(j => j.code)];
    suggestions = [...new Set(candidates)].filter(s => s.toLowerCase().startsWith(prefix.toLowerCase()) && s !== prefix).slice(0, 5);
    suggestionIndex = 0; renderSuggestions();
  }
  function renderSuggestions() {
    const box = $("suggestions"); box.replaceChildren(); box.hidden = !suggestions.length;
    suggestions.forEach((text, i) => {
      const button = document.createElement("button"); button.type = "button"; button.role = "option";
      button.id = "exec-option-" + i; button.tabIndex = -1; button.setAttribute("aria-selected", String(i === suggestionIndex));
      button.textContent = text; button.onmousedown = e => e.preventDefault(); button.onclick = () => acceptSuggestion(i);
      box.append(button);
    });
    if (suggestions.length) {
      $("code").setAttribute("aria-activedescendant", "exec-option-" + suggestionIndex);
      box.children[suggestionIndex].scrollIntoView({ block: "nearest" });
    } else $("code").removeAttribute("aria-activedescendant");
  }
  function acceptSuggestion(index) {
    $("code").setRangeText(suggestions[index], suggestionStart, $("code").selectionStart, "end");
    hideSuggestions(); saveDraft(); position(); updateRun(); $("code").focus();
  }
  $("code").addEventListener("keydown", event => {
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter") { event.preventDefault(); run(); return; }
    if (event.ctrlKey && event.code === "Space") { event.preventDefault(); showSuggestions(true); return; }
    if (event.key === "Escape") { hideSuggestions(); return; }
    if (suggestions.length && ["ArrowDown", "ArrowUp"].includes(event.key)) {
      event.preventDefault();
      suggestionIndex = (suggestionIndex + (event.key === "ArrowDown" ? 1 : suggestions.length - 1)) % suggestions.length;
      renderSuggestions(); return;
    }
    if (event.key === "Tab") {
      if (event.shiftKey) { hideSuggestions(); return; }
      event.preventDefault();
      if (suggestions.length) acceptSuggestion(suggestionIndex);
      else { $("code").setRangeText("  ", $("code").selectionStart, $("code").selectionEnd, "end"); saveDraft(); position(); updateRun(); }
    } else if (["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) hideSuggestions();
  });
  $("code").addEventListener("input", () => { saveDraft(); position(); updateRun(); showSuggestions(); });
  $("code").addEventListener("click", () => { hideSuggestions(); position(); });
  $("code").addEventListener("keyup", position);
  $("code").addEventListener("blur", hideSuggestions);
  $("target").addEventListener("change", () => { message(); selectTarget($("target").value); });
  $("search").addEventListener("input", () => populate(""));
  $("language").addEventListener("change", setLanguage);
  $("timeout").addEventListener("input", updateRun);
  $("run").onclick = run; $("cancel").onclick = cancel;
  $("reconnect").onclick = () => { message(); follow(); };
  $("refresh").onclick = () => refresh(true);
  document.addEventListener("remote-exec-refresh", () => refresh());
  window.addEventListener("pagehide", () => pollController?.abort());
  populate(root.dataset.target);
  if (root.dataset.target && !targets.some(t => t.id === root.dataset.target)) message("The requested HYCO is no longer configured. Select a machine before running.");
  const job = new URLSearchParams(location.search).get("job");
  if (job) {
    api("/v1/jobs/" + encodeURIComponent(job) + "?wait_s=0&max_output_bytes=0&stdout_offset=0&stderr_offset=0")
      .then(view => attach(job, view.target)).catch(error => message("Cannot reopen job: " + error.message));
  }
})();
"""
