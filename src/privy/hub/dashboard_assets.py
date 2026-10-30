"""Static CSS and JavaScript of the hub dashboard (inlined into every page under a CSP nonce)."""

CSS = """
:root{--bg:#f5f6f8;--paper:#fff;--ink:#18232e;--muted:#667481;--faint:#909ba6;--line:#e5e9ed;--accent:#087f78;
--accent-soft:#e8f5f1;--blue:#4975c8;--orange:#b76923;--orange-soft:#fff5e8;--red:#bb5160;--red-soft:#fdeef0;
--purple:#7d65ba;--nav:#132c31;--nav-text:#b6c9cb;--code:#f7f9fb;
--mono:ui-monospace,SFMono-Regular,Consolas,"Liberation Mono",monospace;
--sans:Inter,"Segoe UI",system-ui,-apple-system,sans-serif;font-family:var(--sans);color-scheme:light dark}
@media (prefers-color-scheme:dark){:root{--bg:#10181e;--paper:#17232b;--ink:#e2ebef;--muted:#a0b2bd;--faint:#7f929f;
--line:#2d3c46;--accent:#67c7b7;--accent-soft:#1a3936;--blue:#90b3ef;--orange:#e5ae72;--orange-soft:#382c23;
--red:#f09aa6;--red-soft:#3a2328;--purple:#b5a1e5;--nav:#0c2025;--code:#132029}}
*{box-sizing:border-box}[hidden]{display:none!important}
body{margin:0;background:var(--bg);color:var(--ink);font-size:13px;line-height:1.55}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}h1,h2,h3,p{margin:0}
code,pre,.mono{font-family:var(--mono)}
.sidebar{position:fixed;inset:0 auto 0 0;width:210px;background:var(--nav);color:#fff;padding:28px 16px;
display:flex;flex-direction:column;z-index:20}
.brand b{display:block;font-size:19px;font-weight:650;letter-spacing:-.6px}
.brand small{display:block;font-size:10px;letter-spacing:2px;color:var(--nav-text);margin-top:2px}
.nav-label{margin:36px 10px 12px;color:#739399;letter-spacing:1.6px;font-size:9px;font-weight:700}
.sidebar nav{display:grid;gap:4px}
.sidebar nav a{color:var(--nav-text);padding:9px 10px;border-radius:7px;font-size:12px;display:flex;
justify-content:flex-start;align-items:center;gap:9px}
.sidebar nav svg{width:16px;height:16px;flex-shrink:0;stroke-linecap:round;stroke-linejoin:round}
.sidebar nav a:hover,.sidebar nav a.on{background:#284247;color:#fff;text-decoration:none}
.sidebar nav a span{font-size:9px;color:#97b5b6;background:#ffffff10;padding:1px 6px;border-radius:4px;margin-left:auto}
.sidebar .app{margin-top:auto;padding:14px 10px 0;border-top:1px solid #ffffff17;font:9px/1.8 var(--mono);
color:#7c9c9f;overflow-wrap:anywhere}
.shell{margin-left:210px}.nw{white-space:nowrap}
.topbar{position:sticky;top:0;height:58px;padding:0 34px;background:var(--paper);border-bottom:1px solid var(--line);
display:flex;justify-content:space-between;align-items:center;z-index:10;gap:12px}
.crumbs{color:var(--muted);font-size:11px}.crumbs b{color:var(--ink);font:600 11px var(--mono)}
.main{width:100%;padding:30px 34px 40px}
@media (max-width:860px){.sidebar{position:static;width:auto;padding:18px 16px}.sidebar .app{display:none}
.shell{margin-left:0}.main{padding:22px 16px 32px}.topbar{padding:0 16px}.stats{grid-template-columns:1fr!important}
.logs{grid-template-columns:1fr!important}}
.eyebrow{text-transform:uppercase;color:var(--muted);font-size:9px;font-weight:700;letter-spacing:1.6px}
.hero h1{font-size:34px;font-weight:600;letter-spacing:-1.2px;line-height:1.2;margin:12px 0 14px;overflow-wrap:anywhere}
.chips{display:flex;flex-wrap:wrap;gap:7px;align-items:center}
.chip{display:inline-flex;align-items:center;gap:5px;padding:3px 8px;border:1px solid var(--line);background:var(--paper);
border-radius:5px;font-size:10px;font-weight:550;white-space:nowrap;color:var(--ink)}
.chip.mono{font:500 10px var(--mono)}
.chip.ok{color:var(--accent);background:var(--accent-soft);border-color:transparent}
.chip.warn{color:var(--orange);background:var(--orange-soft);border-color:transparent}
.chip.bad{color:var(--red);background:var(--red-soft);border-color:transparent}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:6px;overflow:hidden}
.seg a{padding:4px 11px;font:600 10px var(--mono);color:var(--muted);border-right:1px solid var(--line);cursor:pointer}
.seg a:last-child{border:0}.seg a.on{background:var(--accent);color:#fff;text-decoration:none}
.stats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px;margin:24px 0 18px}
@media (max-width:1100px){.stats{grid-template-columns:repeat(2,minmax(0,1fr))}.two{grid-template-columns:1fr!important}}
.stat{background:var(--paper);border:1px solid var(--line);border-radius:9px;padding:17px 19px 15px}
.stat-label{color:var(--muted);font-size:10px;font-weight:550}
.stat-value{font-size:28px;line-height:1.3;margin:9px 0 4px;letter-spacing:-.8px;font-weight:600;
font-variant-numeric:tabular-nums}
.stat-value small{font-size:15px;color:var(--muted);font-weight:450}.stat-note{font-size:10px;color:var(--faint)}
.status{padding:13px 19px;border-radius:8px;border:1px solid var(--line);background:var(--paper);margin-bottom:20px}
.status.warn{background:var(--orange-soft)}.status b{font-size:12px}.status p{color:var(--muted);font-size:11px;margin-top:4px}
details.section{background:var(--paper);border:1px solid var(--line);border-radius:10px;margin:16px 0}
details.section>summary{display:flex;align-items:baseline;gap:10px;cursor:pointer;padding:17px 21px;list-style:none}
details.section[open]>summary{border-bottom:1px solid var(--line)}
summary::-webkit-details-marker{display:none}
.num-label{color:var(--faint);font:10px var(--mono)}.title{font-weight:650;font-size:14px}
.subtitle{color:var(--faint);font-size:10px}
details>summary .caret{margin-left:auto;color:var(--faint);transition:transform .15s}
details[open]>summary .caret{transform:rotate(90deg)}
.body{padding:19px 21px}.two{display:grid;grid-template-columns:1fr 1fr;gap:24px}
h3{font-size:12px;font-weight:600;margin:0 0 12px}
.table{overflow:auto;border:1px solid var(--line);border-radius:6px}
table{border-collapse:collapse;width:100%;font-size:11px;font-variant-numeric:tabular-nums}
th{font-weight:600;color:var(--muted);background:var(--code);font-size:10px;position:sticky;top:0;white-space:nowrap;
text-align:left;user-select:none}
th[data-sort]{cursor:pointer}th[data-sort]:hover{color:var(--ink)}th .s{color:var(--accent);margin-left:3px}
td,th{padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:middle}
tr:last-child td{border-bottom:0}tbody tr:hover td{background:var(--code)}tr[data-href]{cursor:pointer}
td.r,th.r{text-align:right;font-family:var(--mono);white-space:nowrap}td.m{font-family:var(--mono)}
td.idx{color:var(--faint);font-family:var(--mono)}td.cmd{font-family:var(--mono);max-width:min(48vw,1100px);overflow:hidden;
text-overflow:ellipsis;white-space:nowrap}
td .sub{display:block;color:var(--faint);font-size:10px}td.hot{color:var(--orange)}
.dot{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:6px;background:var(--accent)}
.dot.off{background:var(--faint)}.dot.bad{background:var(--red)}
svg.spark{display:block}svg.spark .l{fill:none;stroke:var(--accent);stroke-width:1.5}
svg.spark .a{fill:var(--accent);opacity:.12}svg.spark .e{fill:var(--red)}svg.spark .z{stroke:var(--line);stroke-width:1}
svg.bar{display:block;margin-top:5px}svg.bar .t{fill:var(--bg)}svg.bar .f{fill:var(--accent)}svg.bar .f.hot{fill:var(--orange)}
.chart{background:var(--code);border:1px solid var(--line);border-radius:7px;padding:12px}
.chart .grid{stroke:var(--line);stroke-dasharray:3 4}.chart .axis{fill:var(--faint);font:9px var(--mono)}
.chart .exec{fill:var(--accent);opacity:.55}.chart .transfer{fill:var(--blue);opacity:.55}
.chart .sync{fill:var(--purple);opacity:.55}.chart .fail{fill:var(--red)}
.rank{display:grid;grid-template-columns:24px minmax(0,1fr) 150px;gap:9px;align-items:center;padding:8px 0;
border-bottom:1px solid var(--line);color:var(--ink)}
.rank .i{color:var(--faint);font:10px var(--mono)}
.rank .n{display:block;font:600 11px var(--mono);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.rank .v{text-align:right;font:10px var(--mono);color:var(--muted)}
.legend{display:flex;flex-wrap:wrap;gap:14px;font-size:10px;color:var(--muted);margin-top:10px}
.legend i{display:inline-block;width:8px;height:8px;border-radius:2px;margin-right:5px;vertical-align:middle}
.legend .exec{background:var(--accent)}.legend .transfer{background:var(--blue)}.legend .sync{background:var(--purple)}
.legend .fail{background:var(--red)}
.st{font:700 9px var(--mono);text-transform:uppercase;color:var(--accent)}.st.run{color:var(--orange)}
.st.bad{color:var(--red)}.st.off{color:var(--faint)}
.empty{color:var(--faint);font-size:11px;padding:18px 0;text-align:center}
.logs{display:grid;grid-template-columns:260px minmax(0,1fr);gap:16px;margin-top:20px}
.pods{background:var(--paper);border:1px solid var(--line);border-radius:10px;overflow:hidden;align-self:start;
position:sticky;top:76px;max-height:calc(100vh - 96px);overflow-y:auto}
.pods .hd{padding:12px 14px;border-bottom:1px solid var(--line);font-size:10px;color:var(--muted)}
.pods input{width:100%;padding:6px 9px;border:1px solid var(--line);border-radius:6px;background:var(--code);
color:var(--ink);font:11px var(--mono);margin-top:8px}
.pods a{display:grid;grid-template-columns:minmax(0,1fr) auto;padding:9px 14px;border-bottom:1px solid var(--line);
color:var(--ink)}
.pods a:hover{background:var(--code);text-decoration:none}
.pods a.on{background:var(--accent-soft);box-shadow:inset 3px 0 var(--accent)}
.pods a b{font:600 11px var(--mono);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pods a span{font:10px var(--mono);color:var(--faint)}.pods a small{grid-column:1/-1;color:var(--faint);font-size:10px}
.toolbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:12px}
.toolbar>input{flex:1;min-width:200px;padding:6px 9px;border:1px solid var(--line);border-radius:6px;
background:var(--paper);color:var(--ink);font:11px var(--mono)}
label.tg{font-size:10px;color:var(--muted);display:flex;gap:5px;align-items:center;cursor:pointer}
details.term{margin:10px 0;border-radius:9px;overflow:hidden;background:#0d1b1f;border:1px solid #1f3439;
box-shadow:0 1px 2px #0002}
details.term>summary{display:grid;grid-template-columns:auto minmax(120px,260px) minmax(0,1fr) auto;gap:12px;
align-items:center;padding:7px 12px;background:#1a2e33;cursor:pointer;list-style:none;font:11px var(--mono);color:#a9c2c4}
details.term .lights i{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:5px;background:#ff5f57}
details.term .lights i:nth-child(2){background:#febc2e}details.term .lights i:nth-child(3){background:#28c840}
details.term:not([open]) .lights i{filter:grayscale(.6) opacity(.7)}
.term .ttl{color:#7f9ea3;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.term .cmdp{color:#e4f1ef;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.term .meta-r{display:flex;gap:10px;align-items:center;color:#6f9097;font-size:10px;white-space:nowrap}
.term .pill{padding:1px 7px;border-radius:4px;background:#ffffff12;color:#9fd8cd;font-weight:600}
.term .pill.ok{color:#5fd3a5}.term .pill.bad{color:#ff8b98;background:#ff5f5720}
.term .pill.run{color:#ffc773;background:#febc2e1c}
details.term.run{box-shadow:inset 3px 0 #febc2e}details.term.bad{box-shadow:inset 3px 0 #ff5f57}
details.term:target{outline:2px solid var(--accent)}
pre.screen{margin:0;padding:12px 16px 14px;max-height:420px;overflow:auto;color:#cfe3e0;font:11.5px/1.65 var(--mono);
white-space:pre-wrap;word-break:break-word}
pre.screen .pr{color:#5fd3a5;font-weight:600}pre.screen .pa{color:#7aa7ff;font-weight:600}
pre.screen .cm{color:#fff}pre.screen .err{color:#ff8b98}pre.screen .ex{color:#5f7f86}pre.screen a{color:#7aa7ff}
pre.screen .cur{display:inline-block;width:7px;height:14px;background:#cfe3e0;vertical-align:-2px;margin-left:2px;
animation:blink 1s steps(1) infinite}
@keyframes blink{50%{opacity:0}}
.fine{color:var(--faint);font-size:10px;line-height:1.7;margin-top:12px}
.more{display:block;text-align:center;padding:10px;font-size:11px}
.purpose-cell{min-width:320px;max-width:340px}
.purpose-trigger{display:block;border:0;background:transparent;color:var(--ink);text-align:left;
padding:0;cursor:pointer;max-width:310px;font:inherit}
.purpose-sentence{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-weight:600;color:var(--accent)}
.purpose-meta,.purpose-unknown-note{display:block;font-size:9px;color:var(--muted);margin-top:3px}
.purpose-unknown-note{color:var(--orange)}
.purpose-bar{display:block;width:300px;max-width:100%;margin-top:7px;overflow:visible}
.purpose-segment{cursor:pointer}.purpose-segment rect{stroke:var(--paper);stroke-width:1}
.purpose-segment text{fill:white;text-anchor:middle;font:8px var(--mono);pointer-events:none}
.purpose-segment:hover rect,.purpose-segment:focus rect{stroke:var(--ink);stroke-width:2}
.pc0{fill:#087f78}.pc1{fill:#4975c8}.pc2{fill:#9662ac}.pc3{fill:#b77732}
.pc4{fill:#3e889e}.pc5{fill:#b56179}.pc6{fill:#758549}.pc7{fill:#667889}
.purpose-unknown{fill:url(#purpose-gap)}.purpose-mixed{fill:url(#purpose-mix)}
.purpose-pattern-bg{fill:var(--line)}.purpose-pattern-line{stroke:var(--muted);stroke-width:1}
.purpose-axis{display:flex;justify-content:space-between;font:8px var(--mono);color:var(--muted);margin-top:4px}
.purpose-panel{position:fixed;z-index:60;width:420px;max-width:calc(100vw - 24px);max-height:calc(100vh - 24px);
display:flex;flex-direction:column;background:var(--paper);border:1px solid var(--line);border-radius:10px;
box-shadow:0 12px 45px #102e3130;color:var(--ink);overflow:hidden}
.purpose-panel.pinned{inset:0 0 0 auto!important;width:460px;max-width:100vw;height:100vh;max-height:100vh;border-radius:0}
.purpose-panel header{display:flex;align-items:start;gap:12px;padding:17px;border-bottom:1px solid var(--line)}
.purpose-panel h2{font-size:15px}.purpose-target{font:10px var(--mono);overflow-wrap:anywhere;color:var(--muted);margin-top:4px}
.purpose-panel button{cursor:pointer;border:1px solid var(--line);background:var(--paper);color:var(--accent);
padding:5px 9px;border-radius:5px;font:11px var(--sans)}
.purpose-panel [data-purpose-close]{margin-left:auto}
.purpose-selection{padding:10px 17px;border-bottom:1px solid var(--line);background:var(--code);font-size:11px;overflow-wrap:anywhere}
.purpose-selection p{color:var(--muted);font-size:10px;margin-top:5px}
.purpose-entries{overflow:auto;overscroll-behavior:contain;min-height:60px;max-height:330px;padding:12px 18px}
.purpose-panel.pinned .purpose-entries{flex:1;max-height:none}
.purpose-entry{border-left:2px solid var(--line);padding:0 0 18px 14px;overflow-wrap:anywhere}
.purpose-entry.selected{border-left-color:var(--accent);background:var(--accent-soft)}
.purpose-entry time{font:9px var(--mono);color:var(--muted)}
.purpose-entry h3{margin:5px 0;font-size:12px}.purpose-entry p{font-size:10px;color:var(--muted)}
.purpose-panel footer{padding:12px 17px;border-top:1px solid var(--line);font-size:10px;color:var(--muted)}
.purpose-panel footer p{margin-bottom:8px}.purpose-panel [role=alert]{padding:0 17px;color:var(--red);font-size:11px}
.purpose-panel .purpose-loading{padding:4px 17px;color:var(--muted);font-size:11px}
.purpose-trigger:focus-visible,.purpose-panel button:focus-visible{outline:2px solid var(--accent);outline-offset:3px}
"""

JS = r"""
(() => {
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const store = (k, d) => { try { const v = localStorage.getItem(k); return v === null ? d : v === "1"; } catch { return d; } };
  const save = (k, v) => { try { localStorage.setItem(k, v ? "1" : "0"); } catch {} };
  const live = new Map();

  function cellKey(row, i) {
    const c = row.cells[i];
    if (!c) return "";
    const v = c.dataset.v !== undefined ? c.dataset.v : c.textContent.trim();
    return v !== "" && /^-?\d+(\.\d+)?$/.test(v) ? parseFloat(v) : v.toLowerCase();
  }
  function sortTable(th, dir) {
    const table = th.closest("table"), body = table.tBodies[0];
    const i = [...th.parentNode.children].indexOf(th);
    dir = dir || (th.dataset.dir === "desc" ? "asc" : "desc");
    $$("th", table).forEach(x => { delete x.dataset.dir; const s = $(".s", x); if (s) s.textContent = ""; });
    th.dataset.dir = dir;
    const s = $(".s", th); if (s) s.textContent = dir === "desc" ? "\u25BC" : "\u25B2";
    const rows = [...body.rows];
    rows.sort((a, b) => {
      const x = cellKey(a, i), y = cellKey(b, i);
      const c = typeof x === "number" && typeof y === "number" ? x - y : String(x).localeCompare(String(y));
      return dir === "desc" ? -c : c;
    });
    rows.forEach((r, n) => { body.appendChild(r); const idx = $("td.idx", r); if (idx) idx.textContent = String(n + 1).padStart(2, "0"); });
    if (table.id) sessionStorage.setItem("sort:" + table.id, i + ":" + dir);
  }
  function restoreSort() {
    $$("table[id]").forEach(t => {
      const v = sessionStorage.getItem("sort:" + t.id); if (!v) return;
      const [i, dir] = v.split(":"); const th = t.tHead && t.tHead.rows[0].cells[+i];
      if (th && th.dataset.sort !== undefined) sortTable(th, dir);
    });
  }
  function applyFilters() {
    const q = ($("#grep") ? $("#grep").value : "").toLowerCase();
    const on = $(".seg.kind a.on"), k = on ? on.dataset.k : "all";
    $$("details.term").forEach(d => {
      const kind = k === "all" || (k === "failed" ? d.classList.contains("bad") : d.dataset.kind === k);
      d.hidden = !(kind && (!q || d.textContent.toLowerCase().includes(q)));
    });
    const p = ($("#podq") ? $("#podq").value : "").toLowerCase();
    $$(".pods a").forEach(a => { a.hidden = !!p && !a.textContent.toLowerCase().includes(p); });
    filterHycos();
  }
  function filterHycos() {
    const box = $("#hycoq"), table = $("#hycos");
    if (!box || !table) return;
    const terms = box.value.toLowerCase().split(/\s+/).filter(Boolean);
    const st = $(".seg.st a.on"), s = st ? st.dataset.s : "all";
    let shown = 0, up = 0, total = 0;
    $$("tbody tr", table).forEach(tr => {
      total++;
      const hay = tr.dataset.q || "", isUp = tr.dataset.up === "1";
      const ok = terms.every(t => hay.includes(t)) && (s === "all" || (s === "up") === isUp);
      tr.hidden = !ok;
      if (ok) { shown++; if (isUp) up++; }
    });
    const n = $("#hycocount");
    if (n) n.textContent = (shown === total ? shown + " HYCOs" : shown + " of " + total + " shown") +
      " \u00B7 " + up + " up \u00B7 " + (shown - up) + " down";
    const none = $("#hyconone"); if (none) none.hidden = shown > 0;
    const url = new URL(location.href);
    if (box.value) url.searchParams.set("q", box.value); else url.searchParams.delete("q");
    if (s !== "all") url.searchParams.set("status", s); else url.searchParams.delete("status");
    if (url.href !== location.href) history.replaceState(null, "", url);
  }
  function tail(pre) { pre.scrollTop = pre.scrollHeight; }

  async function stream(d, ctl) {
    const out = $(".out", d), pre = $("pre", d);
    let res;
    try {
      res = await fetch("/v1/jobs/" + encodeURIComponent(d.dataset.job) + "/stream",
        { headers: { "X-Privy-UI": "1" }, signal: ctl.signal });
    } catch { return; }
    if (!res.ok || !res.body) return;
    const reader = res.body.getReader(), dec = new TextDecoder();
    let text = "", head = true;
    for (;;) {
      let chunk;
      try { chunk = await reader.read(); } catch { break; }
      if (chunk.done) break;
      text += dec.decode(chunk.value, { stream: true });
      if (head) { const nl = text.indexOf("\n"); if (nl < 0) continue; text = text.slice(nl + 1); head = false; }
      const stick = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 60;
      out.textContent = text;
      if (stick) tail(pre);
    }
  }
  function follow() {
    const box = $("#follow");
    if (!box || !box.checked) { live.forEach(c => c.abort()); live.clear(); return; }
    $$("details.term[data-live]").forEach(d => {
      const id = d.dataset.job;
      if (live.has(id)) return;
      const ctl = new AbortController();
      live.set(id, ctl);
      d.dataset.following = "1";
      stream(d, ctl).finally(() => {
        live.delete(id); delete d.dataset.following;
        if (!ctl.signal.aborted) setTimeout(refresh, 300);
      });
    });
  }
  function init() {
    const f = $("#follow"); if (f) f.checked = store("privy.follow", true);
    const params = new URLSearchParams(location.search), hq = $("#hycoq");
    if (hq && params.get("q")) hq.value = params.get("q");
    if (params.get("status")) $$(".seg.st a").forEach(a => a.classList.toggle("on", a.dataset.s === params.get("status")));
    restoreSort();
    applyFilters();
    $$("details.term[open] pre").forEach(tail);
    follow();
  }

  let busy = false;
  async function refresh() {
    if (document.querySelector("#purpose-panel:not([hidden])")) return;
    if ($("#remote-exec")) {
      document.dispatchEvent(new Event("remote-exec-refresh"));
      return;
    }
    if (busy) return;
    busy = true;
    try {
      const res = await fetch(location.href, { headers: { "X-Privy-UI": "1", "Accept": "text/html" } });
      if (!res.ok) return;
      const html = (await res.text()).replace(/<(style|script)\b[\s\S]*?<\/\1>/gi, "");
      if (document.querySelector("#purpose-panel:not([hidden])")) return;
      const doc = new DOMParser().parseFromString(html, "text/html");
      const open = new Map($$("details[id]").map(d => [d.id, d.open]));
      const keep = new Map($$("details.term[data-following]").map(d => [d.id, d]));
      const scroll = new Map($$("details.term[id] pre").map(p => [p.closest("details").id,
        p.scrollHeight - p.scrollTop - p.clientHeight < 30 ? -1 : p.scrollTop]));
      const values = new Map($$("#main input[id]").map(i => [i.id, i.type === "checkbox" ? i.checked : i.value]));
      const kind = $(".seg.kind a.on"), st = $(".seg.st a.on");
      for (const id of ["main", "nav", "topchips", "appinfo"]) {
        const a = document.getElementById(id), b = doc.getElementById(id);
        if (a && b) a.replaceChildren(...b.childNodes);
      }
      keep.forEach((node, id) => { const n = document.getElementById(id); if (n) n.replaceWith(node); });
      open.forEach((v, id) => { const n = document.getElementById(id); if (n && n.tagName === "DETAILS") n.open = v; });
      values.forEach((v, id) => { const i = document.getElementById(id); if (!i) return; if (i.type === "checkbox") i.checked = v; else i.value = v; });
      if (kind) $$(".seg.kind a").forEach(a => a.classList.toggle("on", a.dataset.k === kind.dataset.k));
      if (st) $$(".seg.st a").forEach(a => a.classList.toggle("on", a.dataset.s === st.dataset.s));
      restoreSort();
      applyFilters();
      $$("details.term[id] pre").forEach(p => {
        const id = p.closest("details").id;
        if (keep.has(id)) return;
        const s = scroll.has(id) ? scroll.get(id) : -1;
        if (s < 0) tail(p); else p.scrollTop = s;
      });
      follow();
    } catch {} finally { busy = false; }
  }

  document.addEventListener("click", e => {
    const th = e.target.closest("th[data-sort]");
    if (th) { sortTable(th); return; }
    const seg = e.target.closest(".seg.kind a");
    if (seg) { e.preventDefault(); $$(".seg.kind a").forEach(a => a.classList.toggle("on", a === seg)); applyFilters(); return; }
    const sseg = e.target.closest(".seg.st a");
    if (sseg) { e.preventDefault(); $$(".seg.st a").forEach(a => a.classList.toggle("on", a === sseg)); applyFilters(); return; }
    const tr = e.target.closest("tr[data-href]");
    if (tr && !e.target.closest("a,button,[role=button]") && !getSelection().toString()) location.href = tr.dataset.href;
  });
  document.addEventListener("input", e => { if (e.target.matches("#grep, #podq, #hycoq")) applyFilters(); });
  document.addEventListener("change", e => {
    const t = e.target;
    if (t.id === "expand") $$("details.term").forEach(d => { d.open = t.checked; });
    if (t.id === "follow") { save("privy.follow", t.checked); follow(); }
    if (t.id === "autorefresh") save("privy.refresh", t.checked);
  });
  document.addEventListener("toggle", e => {
    if (e.target.matches && e.target.matches("details.term") && e.target.open) tail($("pre", e.target));
  }, true);

  const auto = $("#autorefresh");
  if (auto) auto.checked = store("privy.refresh", true);
  setInterval(() => {
    const typing = document.activeElement && document.activeElement.matches("input[type=search]");
    if (auto && auto.checked && !document.hidden && !typing) refresh();
  }, 10000);
  if (location.hash) { const d = document.getElementById(location.hash.slice(1)); if (d && d.tagName === "DETAILS") d.open = true; }
  init();
})();

(() => {
  let panel, trigger, target = "", pinned = false, cursor = null, controller, hideTimer, hoverTimer;
  const date = value => new Date(value * 1000).toISOString().replace("T", " ").replace(/\.\d+Z$/, " UTC");
  const make = (tag, text, cls) => {
    const node = document.createElement(tag); if (text !== undefined) node.textContent = text;
    if (cls) node.className = cls; return node;
  };
  function ensurePanel() {
    if (panel) return;
    panel = make("section", undefined, "purpose-panel");
    panel.id = "purpose-panel"; panel.hidden = true; panel.tabIndex = -1;
    panel.setAttribute("role", "dialog"); panel.setAttribute("aria-label", "Purpose history");
    panel.innerHTML = '<header><div><h2>Purpose history</h2><p class="purpose-target"></p></div>' +
      '<button type="button" data-purpose-close aria-label="Close purpose history">Close</button></header>' +
      '<div class="purpose-selection" hidden></div><p class="purpose-loading" role="status"></p>' +
      '<p role="alert" hidden></p><div class="purpose-entries" tabindex="0" aria-label="Scrollable purpose history"></div>' +
      '<footer><p>Grouped reports, newest first. In memory only; resets on restart. Auto-refresh is paused while this is open.</p>' +
      '<button type="button" data-purpose-more hidden>Load older</button> ' +
      '<button type="button" data-purpose-refresh>Refresh history</button> ' +
      '<button type="button" data-purpose-pin>Pin full history</button></footer>';
    document.body.append(panel);
    panel.querySelector("[data-purpose-close]").onclick = () => close(true);
    panel.querySelector("[data-purpose-pin]").onclick = pin;
    panel.querySelector("[data-purpose-more]").onclick = () => load(true);
    panel.querySelector("[data-purpose-refresh]").onclick = () => load(false);
    panel.addEventListener("pointerenter", () => clearTimeout(hideTimer));
    panel.addEventListener("pointerleave", scheduleClose);
    panel.addEventListener("focusin", () => clearTimeout(hideTimer));
    panel.addEventListener("focusout", scheduleClose);
  }
  function position() {
    if (!panel || panel.hidden || pinned || !trigger) return;
    const r = trigger.getBoundingClientRect(), width = panel.offsetWidth, height = panel.offsetHeight;
    panel.style.left = Math.max(12, Math.min(r.left, innerWidth - width - 12)) + "px";
    const below = r.bottom + 8;
    panel.style.top = Math.max(12, Math.min(below + height <= innerHeight - 12 ? below : r.top - height - 8,
      innerHeight - height - 12)) + "px";
  }
  function pin() {
    pinned = true; panel.classList.add("pinned");
    panel.querySelector("[data-purpose-pin]").hidden = true; panel.focus();
  }
  function close(restore) {
    clearTimeout(hideTimer); clearTimeout(hoverTimer);
    if (!panel) return;
    controller?.abort(); panel.hidden = true; pinned = false; target = "";
    if (trigger) trigger.setAttribute("aria-expanded", "false");
    if (restore && trigger?.isConnected) {
      trigger.focus({ preventScroll: true });
      clearTimeout(hoverTimer);
    }
  }
  function scheduleClose() {
    clearTimeout(hideTimer);
    if (!pinned) hideTimer = setTimeout(() => {
      if (!panel?.contains(document.activeElement)) close(false);
    }, 250);
  }
  function selection(node) {
    const box = panel.querySelector(".purpose-selection");
    box.replaceChildren(); box.hidden = !node.dataset.purposeSegment;
    const s = box.hidden ? null : JSON.parse(node.dataset.purposeSegment);
    panel.querySelectorAll(".purpose-entry").forEach(item =>
      item.classList.toggle("selected", item.dataset.purposeGroup === String(s?.id)));
    if (box.hidden) return;
    box.append(make("b", s.purpose || (s.kind === "mixed" ? "Multiple purpose changes" : "Unknown: no purpose supplied")));
    box.append(make("p", date(s.start) + " to " + date(s.end)));
    if (s.count) box.append(make("p", s.count + " matching report(s); first " + date(s.first_at) +
      ", last " + date(s.last_at) + ". Not continuous activity."));
    else box.append(make("p", s.kind === "mixed" ? "Open the exact history below to inspect changes." :
      "No purpose was reported for this interval. The preceding label is not carried into it."));
  }
  function show(node, keep) {
    clearTimeout(hideTimer); ensurePanel();
    const changed = target !== node.dataset.purposeTarget || panel.hidden;
    if (trigger && trigger !== node) trigger.setAttribute("aria-expanded", "false");
    trigger = node; target = node.dataset.purposeTarget;
    panel.querySelector(".purpose-target").textContent = target;
    panel.hidden = false; node.setAttribute("aria-expanded", "true");
    if (!pinned) panel.classList.remove("pinned");
    panel.querySelector("[data-purpose-pin]").hidden = pinned;
    selection(node); position();
    if (keep) pin();
    if (changed) load(false);
  }
  async function load(append) {
    controller?.abort(); const request = new AbortController(); controller = request;
    const entries = panel.querySelector(".purpose-entries"), error = panel.querySelector("[role=alert]");
    const more = panel.querySelector("[data-purpose-more]"), status = panel.querySelector("[role=status]");
    if (!append) { entries.replaceChildren(); cursor = null; more.hidden = true; }
    error.hidden = true; status.textContent = "Loading purpose history..."; more.disabled = true;
    const range = new URLSearchParams(location.search).get("range") || "24h";
    const params = new URLSearchParams({ target, range, limit: "50" });
    if (append && cursor) params.set("cursor", cursor);
    try {
      const response = await fetch("/v1/purposes?" + params, { signal: request.signal });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error?.message || "History request failed (" + response.status + ")");
      if (!Array.isArray(result.groups)) throw new Error("Invalid purpose history response.");
      if (request.signal.aborted) return;
      const selected = trigger?.dataset.purposeSegment ? JSON.parse(trigger.dataset.purposeSegment).id : null;
      for (const group of result.groups) {
        const item = make("article", undefined, "purpose-entry" + (group.id === selected ? " selected" : ""));
        item.dataset.purposeGroup = String(group.id);
        item.append(make("time", date(group.last_at)));
        item.append(make("h3", (group.label ? group.label + " / " : "") + (group.purpose || "Unknown: purpose omitted")));
        item.append(make("p", group.count + " report(s); first " + date(group.first_at) + ", last " + date(group.last_at)));
        entries.append(item);
      }
      if (!append && !result.groups.length) entries.append(make("p", "No reports in this window since the hub started."));
      cursor = result.next_cursor; more.hidden = !cursor; status.textContent = "";
    } catch (failure) {
      if (failure.name !== "AbortError") {
        error.hidden = false; error.textContent = failure.message + " Use Refresh history to retry.";
        status.textContent = "";
      }
    } finally {
      if (!request.signal.aborted) { more.disabled = false; position(); }
    }
  }
  document.addEventListener("pointerover", e => {
    const node = e.target.closest("[data-purpose-target]");
    if (!node || node.contains(e.relatedTarget) || pinned) return;
    clearTimeout(hideTimer); clearTimeout(hoverTimer); hoverTimer = setTimeout(() => show(node, false), 120);
  });
  document.addEventListener("pointerout", e => {
    const node = e.target.closest("[data-purpose-target]");
    if (node && !node.contains(e.relatedTarget)) { clearTimeout(hoverTimer); scheduleClose(); }
  });
  document.addEventListener("focusin", e => {
    const node = e.target.closest("[data-purpose-target]");
    if (node && !pinned) {
      clearTimeout(hoverTimer); hoverTimer = setTimeout(() => show(node, false), 0);
    }
  });
  document.addEventListener("focusout", e => {
    if (e.target.closest("[data-purpose-target]") && !panel?.contains(e.relatedTarget)) scheduleClose();
  });
  document.addEventListener("click", e => {
    const node = e.target.closest("[data-purpose-target]");
    if (node) { e.preventDefault(); clearTimeout(hoverTimer); show(node, true); }
  });
  document.addEventListener("keydown", e => {
    if (e.key === "Escape" && panel && !panel.hidden) { e.preventDefault(); close(true); }
    const node = e.target.closest(".purpose-segment");
    if (node && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); clearTimeout(hoverTimer); show(node, true); }
  });
  window.addEventListener("resize", position);
  document.addEventListener("scroll", e => { if (!panel?.contains(e.target)) position(); }, true);
})();
"""
