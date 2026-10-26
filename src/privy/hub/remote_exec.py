"""Native, persistent remote terminals with self-contained browser assets."""

from __future__ import annotations

import html
import json
from importlib.resources import files
from typing import Any

from privy.hub.registry import ONLINE
from privy.hub.terminals import terminal_languages

TERMINAL_ICON = (
    '<svg class="terminal-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    'stroke-width="1.7" aria-hidden="true"><rect x="3" y="4" width="18" height="16" rx="4"/>'
    '<path d="m7 9 3 3-3 3m6 0h4"/></svg>'
)


def _asset(name: str) -> str:
    return files("privy.hub").joinpath("assets").joinpath(name).read_text(encoding="utf-8")


def execution_choices(info: dict[str, Any]) -> list[str]:
    return terminal_languages(info)


def workspace(service: Any, report: dict[str, Any], selected: str) -> str:
    targets = []
    for row in report["targets"]:
        if row["status"] != ONLINE:
            continue
        probe = service.registry.cached(service.registry.resolve(row["id"]))
        info = probe.info if probe and probe.info else {}
        choices = terminal_languages(info)
        if not choices:
            continue
        system = (info.get("platform") or {}).get("system")
        preferred = "powershell" if system == "Windows" else "bash"
        targets.append(
            {
                "id": row["id"],
                "label": row["label"],
                "host": row["host"],
                "os": row["os"],
                "languages": choices,
                "default": preferred if preferred in choices else choices[0],
            }
        )
    data = html.escape(json.dumps(targets, ensure_ascii=True), quote=True)
    return f"""
<section id="remote-exec" data-target="{html.escape(selected, quote=True)}">
<div id="exec-data" hidden data-targets="{data}"></div>
<div class="exec-heading"><div><div class="eyebrow">A DIRECT LINE TO YOUR MACHINE</div>
<h1>{TERMINAL_ICON}Remote Exec</h1>
<p>A real shell. Your working directory. Right where you left it.</p></div>
<span class="exec-badge"><i></i> PERSISTENT · INTERACTIVE</span></div>
<noscript><p class="status warn">JavaScript is required for interactive terminals.</p></noscript>
<div class="exec-controls">
<label class="exec-field exec-search-field">Find a machine<input id="exec-search" type="search"
 placeholder="Name, host, platform..." autocomplete="off"></label>
<label class="exec-field exec-machine">Terminal-capable HYCO<select id="exec-target"></select></label>
<label class="exec-field">Shell<select id="exec-language"></select></label>
<button type="button" id="exec-refresh" class="exec-button">Refresh</button>
<button type="button" id="exec-connect" class="exec-button primary" disabled>{TERMINAL_ICON}Connect shell</button>
</div>
<p id="exec-machine-note" class="fine"></p>
<div id="exec-message" role="alert" hidden></div>
<div class="exec-terminal-panel">
<div class="exec-terminal-bar"><div class="exec-terminal-title"><span class="exec-traffic" aria-hidden="true">
<i></i><i></i><i></i></span><span id="exec-session-label">Your next session starts here</span></div>
<div class="exec-session-actions"><span id="exec-state" role="status">Ready</span>
<button type="button" id="exec-reconnect" class="exec-button" hidden>Reconnect</button>
<button type="button" id="exec-disconnect" class="exec-button" disabled>Disconnect</button></div></div>
<div id="exec-terminal" aria-label="Interactive remote terminal"></div>
<div class="exec-terminal-foot"><span id="exec-size">80 × 24</span>
<span id="exec-idle">60-minute idle timeout</span><span>PowerShell / Bash · PTY</span></div>
</div>
<div class="exec-bottom"><span>{TERMINAL_ICON}Tab completion and history are provided by your remote shell.</span>
<span>Variables and <code>cd</code> persist. Leaving or refreshing closes the shell.</span></div>
</section>
"""


EXEC_CSS = (
    _asset("xterm.css")
    + """
.terminal-icon{width:18px;height:18px;vertical-align:middle;flex-shrink:0}
.remote-link{display:inline-flex;align-items:center;gap:4px;border:1px solid var(--line);border-radius:5px;
padding:2px 6px;margin-top:5px;font-size:10px;background:var(--accent-soft)}
.remote-link:hover{background:var(--accent);color:#fff;text-decoration:none}.remote-link .terminal-icon{width:13px;height:13px}
.exec-heading{display:flex;justify-content:space-between;align-items:center;gap:20px;margin:6px 0 28px}
.exec-heading h1{display:flex;gap:12px;align-items:center;font-size:32px;letter-spacing:-1.3px;margin:8px 0}
.exec-heading h1 .terminal-icon{width:31px;height:31px;stroke:var(--accent)}
.exec-heading p{margin:6px 0 0;color:var(--muted);font-size:13px}
.exec-badge{display:flex;align-items:center;gap:7px;border:1px solid #c1d7d1;background:#edf5f0;color:#2d6d57;
padding:7px 10px;border-radius:20px;font-size:9px;font-weight:700;letter-spacing:1px;white-space:nowrap}
.exec-badge i{width:6px;height:6px;border-radius:50%;background:#4c9872}
.exec-controls{display:flex;gap:12px;align-items:flex-end;flex-wrap:wrap;padding:18px;background:var(--paper);
border:1px solid var(--line);border-radius:12px;box-shadow:0 3px 16px #102e2304}
.exec-field{display:flex;flex-direction:column;gap:7px;font-size:10px;font-weight:650;color:var(--muted);min-width:140px}
.exec-field input,.exec-field select{box-sizing:border-box;height:36px;border:1px solid var(--line);border-radius:6px;
background:var(--paper);color:var(--ink);padding:0 10px;font:12px var(--sans);width:100%;min-width:0}
.exec-field select:focus,.exec-field input:focus{outline:2px solid #6fa68e;outline-offset:2px}
.exec-machine{flex:1;min-width:220px}.exec-search-field{width:190px}
.exec-button{display:inline-flex;align-items:center;justify-content:center;gap:6px;height:36px;padding:0 13px;
border:1px solid var(--line);border-radius:6px;background:var(--paper);color:var(--ink);font:600 11px var(--sans);
cursor:pointer;white-space:nowrap;transition:background .15s,box-shadow .15s}
.exec-button:hover:not(:disabled){background:var(--accent-soft);box-shadow:0 2px 6px #193b2510}
.exec-button:focus-visible{outline:2px solid #6fa68e;outline-offset:3px}.exec-button:disabled{opacity:.45;cursor:not-allowed}
.exec-button.primary{background:#245e49;color:#fff;border-color:#245e49}
.exec-button.primary:hover:not(:disabled){background:#194b39;color:#fff}
#exec-machine-note{margin:11px 2px 18px;min-height:16px}
#exec-message{border:1px solid #dfc694;border-radius:8px;background:#fff9ed;color:#74521e;padding:12px 15px;
font-size:12px;margin-bottom:15px}#exec-message[hidden]{display:none}
.exec-terminal-panel{overflow:hidden;border:1px solid #21343e;border-radius:12px;background:#101b24;
box-shadow:0 12px 35px #112c2114}
.exec-terminal-bar{display:flex;justify-content:space-between;align-items:center;gap:12px;min-height:49px;padding:8px 15px;
background:#172630;border-bottom:1px solid #263943;color:#9db2b9;font:11px var(--mono);box-sizing:border-box}
.exec-terminal-title{display:flex;gap:15px;align-items:center;min-width:0}
#exec-session-label{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.exec-traffic{display:flex;gap:6px}.exec-traffic i{display:block;width:8px;height:8px;border-radius:50%;background:#42606b}
.exec-traffic i:nth-child(2){background:#57726a}.exec-traffic i:nth-child(3){background:#608575}
.exec-session-actions{display:flex;align-items:center;gap:12px;flex-shrink:0}
.exec-session-actions .exec-button{height:28px;background:#203640;border-color:#36505a;color:#c6dcdd;font-size:10px}
.exec-session-actions .exec-button:hover:not(:disabled){background:#2c4651;color:#fff}
#exec-state{font-size:10px;color:#b3ccbf}#exec-state[data-live="true"]{color:#8ed8ac}
#exec-terminal{box-sizing:border-box;height:min(64vh,730px);min-height:360px;padding:17px 18px 13px;background:#101b24}
#exec-terminal .xterm{height:100%}.xterm .xterm-viewport{scrollbar-width:thin;scrollbar-color:#354c58 #101b24}
.exec-terminal-foot{display:flex;justify-content:space-between;gap:15px;padding:10px 17px;border-top:1px solid #243640;
font:10px var(--mono);color:#829ba6;background:#12212a}
.exec-bottom{display:flex;justify-content:space-between;flex-wrap:wrap;gap:12px;margin:17px 2px;color:var(--muted);font-size:10px}
.exec-bottom .terminal-icon{width:13px;height:13px;margin-right:6px}
@media(max-width:1050px){.exec-heading{align-items:flex-start}.exec-heading h1{font-size:28px}
.exec-badge{display:none}.exec-search-field{width:150px}.exec-machine{min-width:200px}}
@media(max-width:650px){.exec-controls{gap:10px;padding:13px}.exec-field{flex:1;min-width:110px}
.exec-machine{min-width:100%;order:-1}.exec-search-field{min-width:100%}
#exec-terminal{height:60vh;min-height:330px;padding:11px 8px}
.exec-terminal-bar{padding:8px 10px;flex-wrap:wrap}.exec-terminal-title{width:100%}
.exec-session-actions{width:100%;justify-content:flex-end}.exec-terminal-foot{font-size:9px}
.exec-terminal-foot>span:last-child{display:none}.exec-bottom{line-height:1.8}}
"""
)

EXEC_JS = _asset("xterm.js") + "\n" + _asset("xterm-fit.js") + "\n" + _asset("remote_exec.js")
