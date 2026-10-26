(() => {
  "use strict";
  const root = document.querySelector("#remote-exec");
  if (!root) return;
  const $ = id => document.getElementById(id);
  const labels = {powershell: "PowerShell", bash: "Bash"};
  let targets = JSON.parse($("exec-data").dataset.targets);
  let session = null, refreshing = false, leaving = false, resizeTimer = null, userGestureAt = -Infinity;
  const encoder = new TextEncoder();
  const nonce = document.currentScript.nonce;
  // Scope nonce-aware style creation to xterm's documented document override.
  const terminalDocument = new Proxy(document, {
    get(target, property) {
      if (property === "createElement") return (...args) => {
        const element = target.createElement(...args);
        if (element.tagName === "STYLE") element.nonce = nonce;
        return element;
      };
      const value = Reflect.get(target, property, target);
      return typeof value === "function" ? value.bind(target) : value;
    }
  });
  const terminal = new Terminal({
    documentOverride: terminalDocument, cursorBlink: true, cursorStyle: "bar",
    fontFamily: "'Cascadia Code', 'SFMono-Regular', Consolas, monospace",
    fontSize: 13, lineHeight: 1.25, scrollback: 3000, disableStdin: true,
    screenReaderMode: true, allowProposedApi: true, altClickMovesCursor: false,
    linkHandler: {activate: event => event.preventDefault()}, windowOptions: {},
    theme: {
      background: "#101b24", foreground: "#d6e4e6", cursor: "#93d5b5",
      selectionBackground: "#365665", black: "#1b2d38", brightBlack: "#6a858f",
      red: "#ed918a", green: "#91c7a4", yellow: "#dec48a", blue: "#91b4df",
      magenta: "#c5a5d8", cyan: "#87c6c7", white: "#d6e4e6"
    }
  });
  terminal.parser.registerOscHandler(52, () => true);
  const fit = new FitAddon.FitAddon();
  terminal.loadAddon(fit);
  terminal.open($("exec-terminal"));
  const composer = PrivyComposer.create({
    submit: submitCommand,
    focusTerminal: () => terminal.focus(),
    interrupt: () => {
      const s = session;
      if (s?.phase === "connected") {
        try { enqueue(s, "\x03", true); }
        catch (error) { message(error.message); }
      }
    }
  });
  for (const name of ["keydown", "input", "paste"]) {
    $("exec-terminal").addEventListener(name, event => {
      if (event.isTrusted) {
        userGestureAt = performance.now();
      }
    }, true);
  }
  fit.fit();
  terminal.writeln("\x1b[1;38;2;147;213;181m  privy / remote\x1b[0m");
  terminal.writeln("\r\n  Connect to a terminal-capable listener to open a real shell.");
  terminal.writeln("  Your variables, working directory, and shell history stay with you.\r\n");
  terminal.writeln("\x1b[38;2;130;155;166m  Tab completes remotely. Ctrl+C interrupts. Exit closes the session.\x1b[0m");

  function message(text = "") {
    $("exec-message").textContent = text;
    $("exec-message").hidden = !text;
  }
  function controls() {
    $("exec-connect").disabled = !!session || !$("exec-target").value || !$("exec-language").value;
    $("exec-disconnect").disabled = !session;
    $("exec-reconnect").hidden = !session || session.phase !== "interrupted";
    terminal.options.disableStdin = !session || session.phase !== "connected";
    composer.availability(session?.phase === "connected", session?.phase === "interrupted");
  }
  function status(text, live = false) {
    $("exec-state").textContent = text;
    $("exec-state").dataset.live = String(live);
    controls();
  }
  function shellChoices(preferred = "") {
    const target = targets.find(item => item.id === $("exec-target").value);
    $("exec-language").replaceChildren(...(target ? target.languages : []).map(shell => new Option(labels[shell], shell)));
    if (target) $("exec-language").value = target.languages.includes(preferred) ? preferred : target.default;
    composer.context(target?.id || "", $("exec-language").value);
    $("exec-machine-note").textContent = target
      ? `${target.host || target.label} · ${target.os || "Remote host"} · Real interactive shell`
      : "No matching online terminal-capable listeners. Upgrade a listener to enable its Remote action.";
    controls();
  }
  function renderTargets(preferred = "", shell = "") {
    const search = $("exec-search").value.toLowerCase();
    const available = targets.filter(item => `${item.id} ${item.label} ${item.host} ${item.os}`.toLowerCase().includes(search));
    $("exec-target").replaceChildren(...available.map(item => new Option(`${item.label} · ${item.host || item.id}`, item.id)));
    if (available.some(item => item.id === preferred)) $("exec-target").value = preferred;
    shellChoices(shell);
  }
  async function api(path, body) {
    const response = await fetch(path, {
      method: "POST", headers: {"Content-Type": "application/json", "X-Privy-UI": "1"},
      body: JSON.stringify(body), cache: "no-store"
    });
    const result = await response.json();
    if (!response.ok || result.ok === false) {
      const error = new Error(result.error?.message || `Terminal request failed (${response.status})`);
      error.details = result;
      throw error;
    }
    return result;
  }
  function request(s, action, extra = {}) {
    return api(`/v1/terminals/${s.id}/${action}`, {owner: s.owner, target: s.target, ...extra});
  }
  function interrupted(s, error) {
    if (session !== s || leaving || s.phase === "closing") return;
    s.phase = "interrupted";
    status("Connection interrupted");
    message(`${error.message} Your shell has not been replaced. Reconnect to resume, or disconnect to close it.`);
  }
  function updateIdle(s, result) {
    if (Number.isFinite(result.expires_in_s)) s.deadline = performance.now() + result.expires_in_s * 1000;
  }
  function bytesToBase64(bytes) {
    return btoa(String.fromCharCode(...bytes));
  }
  function base64ToBytes(text) {
    return Uint8Array.from(atob(text), character => character.charCodeAt(0));
  }
  async function readLoop(s) {
    if (s.reading) return;
    s.reading = true;
    try {
      while (session === s && s.phase === "connected" && !leaving) {
        const result = await request(s, "read", {offset: s.offset, wait_s: 1, max_bytes: 32768});
        if (session !== s || leaving || s.phase !== "connected") break;
        if (!Number.isSafeInteger(result.offset) || result.offset < s.offset) throw new Error("Invalid terminal output offset.");
        const bytes = base64ToBytes(result.data_b64 || "");
        if (bytes.length) await new Promise(resolve => terminal.write(bytes, resolve));
        s.offset = result.offset;
        updateIdle(s, result);
        if (result.state !== "running") {
          s.phase = "closed";
          session = null;
          terminal.writeln(`\r\n\x1b[38;2;130;155;166m[Shell ${result.state}${result.exit_code == null ? "" : ` · exit ${result.exit_code}`}]\x1b[0m`);
          status(result.state === "expired" ? "Idle timeout" : "Shell closed");
          renderTargets(s.target, s.shell);
          break;
        }
      }
    } catch (error) {
      const gap = error.details;
      if (gap?.error?.code === "buffer_gap" && Number.isSafeInteger(gap.start_offset)) {
        s.offset = gap.start_offset;
        terminal.writeln("\r\n\x1b[33m[Output exceeded the replay buffer. Some output was lost.]\x1b[0m");
      }
      interrupted(s, error);
    } finally { s.reading = false; }
  }
  async function flush(s) {
    if (s.writing || s.phase !== "connected") return;
    s.writing = true;
    try {
      while (session === s && s.phase === "connected" && (s.pending || s.queue.length)) {
        if (!s.pending) {
          const chunks = [];
          let length = 0, activity = false, receipt = null;
          while (s.queue.length && length + s.queue[0].bytes.length <= 16384) {
            const chunk = s.queue.shift();
            chunks.push(chunk.bytes);
            length += chunk.bytes.length;
            activity ||= chunk.activity;
            receipt ||= chunk.receipt;
            if (receipt) break;
          }
          const bytes = new Uint8Array(length);
          let position = 0;
          for (const chunk of chunks) { bytes.set(chunk, position); position += chunk.length; }
          s.pending = {bytes, activity, receipt};
        }
        const data = s.pending;
        const result = await request(s, "write", {
          seq: s.seq, data_b64: bytesToBase64(data.bytes), user_input: data.activity
        });
        if (result.next_seq !== s.seq + 1) throw new Error("Input acknowledgement was not valid. Input has not been replayed.");
        s.pending = null;
        s.queued -= data.bytes.length;
        s.seq = result.next_seq;
        updateIdle(s, result);
        if (session === s && data.receipt) composer.delivered(data.receipt);
      }
    } catch (error) { interrupted(s, error); }
    finally { s.writing = false; }
  }
  function enqueue(s, data, activity, receipt = null) {
    const bytes = encoder.encode(data);
    if (s.queued + bytes.length > 262144) {
      throw new Error("Input queue is full. Wait for the listener before sending more text.");
    }
    for (let begin = 0; begin < bytes.length;) {
      let end = Math.min(begin + 16384, bytes.length);
      while (end < bytes.length && (bytes[end] & 0xc0) === 0x80) end--;
      s.queue.push({bytes: bytes.slice(begin, end), activity, receipt});
      begin = end;
    }
    s.queued += bytes.length;
    clearTimeout(s.flushTimer);
    s.flushTimer = setTimeout(() => void flush(s), 16);
  }
  function submitCommand(text, receipt) {
    const s = session;
    if (!s || s.phase !== "connected") throw new Error("Connect to your shell before running a command.");
    enqueue(s, PrivyComposer.encode(text, s.shell, terminal.modes.bracketedPasteMode), true, receipt);
  }
  terminal.onData(data => {
    const s = session;
    if (!s || s.phase !== "connected") return;
    try { enqueue(s, data, performance.now() - userGestureAt < 1000); }
    catch (error) { message(error.message); }
  });
  async function connect() {
    if (session || leaving) return;
    const target = targets.find(item => item.id === $("exec-target").value);
    const shell = $("exec-language").value;
    if (!target || !target.languages.includes(shell)) return;
    const s = {
      id: crypto.randomUUID(), owner: crypto.randomUUID(), target: target.id, shell,
      offset: 0, seq: 0, queue: [], queued: 0, phase: "connecting", deadline: 0
    };
    session = s;
    message();
    status("Connecting");
    $("exec-session-label").textContent = `${labels[shell]} / ${target.id}`;
    await new Promise(resolve => terminal.write("\x1bc", resolve));
    if (session !== s || leaving || s.phase !== "connecting") return;
    const url = new URL(location.href);
    url.searchParams.set("target", target.id);
    url.searchParams.set("shell", shell);
    history.replaceState(null, "", url);
    try {
      const result = await api("/v1/terminals", {
        target: target.id, shell, owner: s.owner, request_id: s.id, cols: terminal.cols, rows: terminal.rows
      });
      if (session !== s || leaving) return;
      if (result.state !== "running") throw new Error("The remote shell exited while connecting.");
      s.seq = result.next_seq || 0;
      s.phase = "connected";
      updateIdle(s, result);
      status("Connected", true);
      composer.focus();
      void readLoop(s);
    } catch (error) { interrupted(s, error); }
  }
  async function disconnect() {
    const s = session;
    if (!s) return true;
    s.phase = "closing";
    status("Closing");
    try {
      await request(s, "close");
      if (session === s) {
        session = null;
        terminal.writeln("\r\n\x1b[38;2;130;155;166m[Disconnected. Remote shell closed.]\x1b[0m");
        status("Disconnected");
        message();
      }
      return true;
    } catch (error) {
      s.phase = "interrupted";
      status("Cleanup pending");
      message(`${error.message} Retry Disconnect. The listener's 60-minute input-idle timeout remains active.`);
      return false;
    }
  }
  $("exec-connect").addEventListener("click", () => void connect());
  $("exec-disconnect").addEventListener("click", () => void disconnect());
  $("exec-reconnect").addEventListener("click", async () => {
    const s = session;
    if (!s || s.phase !== "interrupted") return;
    s.phase = "connected";
    message();
    status("Connected", true);
    // The listener deduplicates this exact sequence if its acknowledgement was lost.
    await flush(s);
    if (s.phase === "connected") {
      void readLoop(s);
      composer.focus();
    }
  });
  $("exec-target").addEventListener("change", async () => {
    const s = session, selected = $("exec-target").value;
    if (s && !await disconnect()) { $("exec-target").value = s.target; return; }
    $("exec-target").value = selected;
    shellChoices();
    message();
  });
  $("exec-language").addEventListener("change", async () => {
    const s = session;
    if (s && !await disconnect()) $("exec-language").value = s.shell;
    composer.context($("exec-target").value, $("exec-language").value);
  });
  $("exec-search").addEventListener("input", () => {
    if (!session) renderTargets($("exec-target").value, $("exec-language").value);
  });
  async function refresh() {
    if (refreshing || leaving) return;
    refreshing = true;
    $("exec-refresh").disabled = true;
    try {
      const response = await fetch(location.href, {headers: {"Accept": "text/html", "X-Privy-UI": "1"}, cache: "no-store"});
      if (!response.ok) throw new Error(`Target refresh failed (${response.status}).`);
      const markup = (await response.text()).replace(/<(style|script)\b[\s\S]*?<\/\1>/gi, "");
      const doc = new DOMParser().parseFromString(markup, "text/html");
      const data = doc.getElementById("exec-data");
      if (!data) throw new Error("Target refresh returned an invalid workspace.");
      targets = JSON.parse(data.dataset.targets);
      if (!session) renderTargets($("exec-target").value, $("exec-language").value);
    } catch (error) { if (!leaving) message(error.message); }
    finally { refreshing = false; $("exec-refresh").disabled = false; }
  }
  $("exec-refresh").addEventListener("click", () => void refresh());
  document.addEventListener("remote-exec-refresh", () => void refresh());
  const observer = new ResizeObserver(() => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(async () => {
      if (leaving) return;
      fit.fit();
      $("exec-size").textContent = `${terminal.cols} × ${terminal.rows}`;
      const s = session;
      if (s?.phase === "connected") {
        try { await request(s, "resize", {cols: terminal.cols, rows: terminal.rows}); }
        catch (error) { interrupted(s, error); }
      }
    }, 150);
  });
  observer.observe($("exec-terminal"));
  const timer = setInterval(() => {
    const s = session;
    if (!s || !s.deadline) { $("exec-idle").textContent = "60-minute idle timeout"; return; }
    const seconds = Math.max(0, Math.ceil((s.deadline - performance.now()) / 1000));
    $("exec-idle").textContent = `Idle timeout ${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
  }, 1000);
  function leave() {
    if (leaving) return;
    leaving = true;
    const s = session;
    if (s) {
      s.phase = "closing";
      clearTimeout(s.flushTimer);
      const url = `/v1/terminals/${s.id}/close`;
      const body = JSON.stringify({owner: s.owner, target: s.target});
      if (!navigator.sendBeacon(url, new Blob([body], {type: "application/json"}))) {
        void fetch(url, {method: "POST", headers: {"Content-Type": "application/json"}, body, keepalive: true});
      }
    }
    clearInterval(timer);
    clearTimeout(resizeTimer);
    observer.disconnect();
  }
  window.addEventListener("pagehide", leave);
  window.addEventListener("pageshow", event => { if (event.persisted) location.reload(); });
  renderTargets(root.dataset.target, new URL(location.href).searchParams.get("shell") || "");
  if (root.dataset.target) {
    if (targets.some(target => target.id === root.dataset.target)) void connect();
    else {
      $("exec-target").selectedIndex = -1;
      shellChoices();
      message("This listener is offline or does not advertise interactive terminal capability. No shell was opened.");
    }
  }
})();
