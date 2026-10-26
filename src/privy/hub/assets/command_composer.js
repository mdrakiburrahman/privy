const PrivyComposer = (() => {
  "use strict";
  const encoder = new TextEncoder();
  const catalog = {
    powershell: [
      ["Get-ChildItem", "List files and directories", "Get-ChildItem"],
      ["Get-Content", "Read a file", "Get-Content "],
      ["Get-Command", "Discover commands on the remote host", "Get-Command "],
      ["Get-Help", "Read remote command help", "Get-Help "],
      ["Get-Location", "Show the current directory", "Get-Location"],
      ["Set-Location", "Change the persistent working directory", "Set-Location "],
      ["Get-Process", "List processes", "Get-Process"],
      ["Get-Service", "List services", "Get-Service"],
      ["Get-Item", "Inspect a path or variable", "Get-Item "],
      ["Test-Path", "Check whether a path exists", "Test-Path "],
      ["Select-Object", "Select properties from pipeline output", "Select-Object "],
      ["Where-Object", "Filter pipeline output", "Where-Object { $_ }"],
      ["ForEach-Object", "Transform pipeline output", "ForEach-Object {\n    $_\n}"],
      ["Sort-Object", "Sort pipeline output", "Sort-Object "],
      ["Format-Table", "Format objects as a table", "Format-Table -AutoSize"],
      ["ConvertTo-Json", "Serialize pipeline output as JSON", "ConvertTo-Json -Depth 5"],
      ["Write-Output", "Write a value to the pipeline", "Write-Output "],
      ["Read-Host", "Ask for input in the terminal", "Read-Host "],
      ["Invoke-RestMethod", "Call an HTTP API from the remote host", "Invoke-RestMethod "],
      ["Get-Date", "Show the remote date and time", "Get-Date"],
      ["foreach", "Iterate over a collection", "foreach ($item in $items) {\n    $item\n}"],
      ["if", "Conditional statement", "if ($condition) {\n    \n}"],
      ["try", "Handle terminating errors", "try {\n    \n} catch {\n    Write-Error $_\n}"]
    ],
    bash: [
      ["printf", "Print formatted output", "printf"],
      ["echo", "Print a line", "echo "],
      ["pwd", "Show the persistent working directory", "pwd"],
      ["cd", "Change the persistent working directory", "cd "],
      ["ls", "List files and directories", "ls -lah"],
      ["find", "Find paths on the remote host", "find . -name "],
      ["grep", "Search text", "grep -n "],
      ["cat", "Read a file", "cat "],
      ["head", "Read the beginning of a file", "head -n 20 "],
      ["tail", "Read the end of a file", "tail -n 50 "],
      ["ps", "List remote processes", "ps aux"],
      ["df", "Show filesystem space", "df -h"],
      ["du", "Show directory sizes", "du -sh "],
      ["env", "Inspect the shell environment", "env"],
      ["export", "Set an environment variable", "export NAME="],
      ["command", "Discover a remote command", "command -v "],
      ["git", "Inspect a repository", "git status --short"],
      ["curl", "Make an HTTP request from the remote host", "curl -fsS "],
      ["read", "Ask for input in the terminal", "read -r -p 'Value: ' value"],
      ["for", "Iterate over values", "for item in one two; do\n    printf '%s\\n' \"$item\"\ndone"],
      ["if", "Conditional statement", "if [ -n \"$value\" ]; then\n    \nfi"],
      ["while", "Read lines", "while IFS= read -r line; do\n    printf '%s\\n' \"$line\"\ndone"],
      ["function", "Define a persistent function", "function name() {\n    \n}"]
    ]
  };

  function encode(text, shell, bracketedPaste) {
    if (!text.trim()) throw new Error("Enter a command before running it.");
    const bytes = encoder.encode(text);
    if (bytes.length > 8192) throw new Error("Command exceeds the 8 KiB UTF-8 limit. Nothing was sent.");
    if (/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]/u.test(text)) {
      throw new Error("Remove hidden terminal control characters. Nothing was sent.");
    }
    text = text.replace(/\r\n?/g, "\n");
    let input;
    if (!["powershell", "bash"].includes(shell)) throw new Error("Select PowerShell or Bash.");
    if (bracketedPaste) {
      input = `\x1b[200~${text}\x1b[201~\r`;
    } else if (!/[\n\t]/.test(text)) {
      input = `${text}\r`;
    } else if (shell === "powershell") {
      const encoded = btoa(String.fromCharCode(...encoder.encode(text)));
      input = `Invoke-Expression ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('${encoded}')))\r`;
    } else {
      const quoted = text.replace(/\\/g, "\\\\").replace(/'/g, "\\'").replace(/\n/g, "\\n").replace(/\t/g, "\\t");
      input = `eval -- $'${quoted}'\r`;
    }
    if (encoder.encode(input).length > 16384) {
      throw new Error("Encoded command exceeds the terminal batch limit. Shorten the draft; nothing was sent.");
    }
    return input;
  }

  function create({submit, focusTerminal, interrupt}) {
    const $ = id => document.getElementById(id);
    const editor = $("exec-command"), menu = $("exec-suggestions");
    let context = "", shell = "powershell", available = false, pending = false;
    let history = [], historyIndex = 0, savedDraft = "", matches = [], selected = 0, tokenStart = 0;
    let revision = 0, ticket = null, composing = false;

    function delivery(text, state = "") {
      $("exec-delivery").textContent = text;
      $("exec-delivery").dataset.state = state;
    }
    function controls() {
      $("exec-run").disabled = !available || pending;
      $("exec-interrupt").disabled = !available;
      $("exec-focus-terminal").disabled = !available;
      $("exec-history-prev").disabled = !history.length || historyIndex <= 0;
      $("exec-history-next").disabled = historyIndex >= history.length;
      $("exec-bytes").textContent = `${encoder.encode(editor.value).length.toLocaleString()} / 8,192 bytes`;
    }
    function dismiss() {
      matches = [];
      menu.hidden = true;
      editor.setAttribute("aria-expanded", "false");
      editor.removeAttribute("aria-activedescendant");
    }
    function highlight() {
      for (const [index, option] of [...menu.children].entries()) {
        option.setAttribute("aria-selected", String(index === selected));
      }
      editor.setAttribute("aria-activedescendant", `exec-suggestion-${selected}`);
      menu.children[selected]?.scrollIntoView({block: "nearest"});
    }
    function suggest() {
      dismiss();
      if (composing || editor.selectionStart !== editor.selectionEnd) return;
      if (/[\w-]/.test(editor.value[editor.selectionStart] || "")) return;
      const before = editor.value.slice(0, editor.selectionStart);
      const token = before.match(/(?:^|[\s;|&])([a-zA-Z][\w-]*)$/);
      if (!token || token[1].length < 2) return;
      tokenStart = editor.selectionStart - token[1].length;
      matches = (catalog[shell] || []).filter(item => item[0].toLowerCase().startsWith(token[1].toLowerCase())).slice(0, 7);
      if (!matches.length) return;
      selected = 0;
      menu.replaceChildren(...matches.map(([label, detail], index) => {
        const option = document.createElement("li");
        option.id = `exec-suggestion-${index}`;
        option.setAttribute("role", "option");
        const title = document.createElement("strong"), description = document.createElement("span");
        title.textContent = label;
        description.textContent = detail;
        option.append(title, description);
        option.addEventListener("pointerdown", event => { event.preventDefault(); selected = index; accept(); });
        return option;
      }));
      menu.hidden = false;
      editor.setAttribute("aria-expanded", "true");
      highlight();
    }
    function accept() {
      const item = matches[selected];
      if (!item) return;
      editor.focus();
      editor.setSelectionRange(tokenStart, editor.selectionEnd);
      // Chromium's editing command retains native undo history, unlike assigning value.
      if (!document.execCommand("insertText", false, item[2])) {
        editor.setRangeText(item[2], editor.selectionStart, editor.selectionEnd, "end");
        editor.dispatchEvent(new Event("input", {bubbles: true}));
      }
      dismiss();
    }
    function recall(step) {
      if (!history.length) return;
      if (historyIndex === history.length) savedDraft = editor.value;
      historyIndex = Math.max(0, Math.min(history.length, historyIndex + step));
      editor.value = historyIndex === history.length ? savedDraft : history[historyIndex];
      revision++;
      dismiss();
      controls();
      editor.focus();
      editor.setSelectionRange(editor.value.length, editor.value.length);
    }
    function run() {
      if (composing || !available || pending) return;
      const text = editor.value;
      let input;
      try { input = encode(text, shell, false); }
      catch (error) { delivery(error.message, "error"); return; }
      dismiss();
      pending = true;
      ticket = {text, revision};
      delivery("Sending to your existing shell...", "pending");
      controls();
      try {
        // The terminal chooses native bracketed paste only when the shell advertised it.
        submit(text, ticket, input);
      } catch (error) {
        pending = false;
        ticket = null;
        delivery(error.message, "error");
        controls();
      }
    }
    editor.addEventListener("input", () => {
      revision++;
      controls();
      suggest();
      if (!pending) delivery("Draft stays in this browser until Run.");
    });
    editor.addEventListener("compositionstart", () => { composing = true; dismiss(); });
    editor.addEventListener("compositionend", () => { composing = false; suggest(); });
    editor.addEventListener("keydown", event => {
      if (event.isComposing || composing) return;
      if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
        event.preventDefault();
        if (!event.repeat) run();
      } else if (!event.shiftKey && (event.ctrlKey || event.metaKey) && ["ArrowUp", "ArrowDown"].includes(event.key)) {
        event.preventDefault();
        recall(event.key === "ArrowUp" ? -1 : 1);
      } else if (!menu.hidden && !event.shiftKey && ["ArrowUp", "ArrowDown"].includes(event.key)) {
        event.preventDefault();
        selected = (selected + (event.key === "ArrowUp" ? -1 : 1) + matches.length) % matches.length;
        highlight();
      } else if (!menu.hidden && event.key === "Tab" && !event.shiftKey) {
        event.preventDefault();
        accept();
      } else if (event.key === "Escape") {
        dismiss();
      } else if (["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End", "PageUp", "PageDown"].includes(event.key)) {
        dismiss();
      }
    });
    editor.addEventListener("blur", dismiss);
    editor.addEventListener("click", suggest);
    $("exec-run").addEventListener("click", event => { if (event.detail < 2) run(); });
    $("exec-run").addEventListener("keydown", event => {
      if (event.repeat && ["Enter", " "].includes(event.key)) event.preventDefault();
    });
    $("exec-history-prev").addEventListener("click", () => recall(-1));
    $("exec-history-next").addEventListener("click", () => recall(1));
    $("exec-focus-terminal").addEventListener("click", () => { dismiss(); focusTerminal(); });
    $("exec-interrupt").addEventListener("click", interrupt);
    controls();
    return {
      focus() { editor.focus(); },
      context(target, language) {
        const next = `${target}/${language}`;
        shell = language;
        $("exec-composer-shell").textContent = language === "powershell" ? "PowerShell" : "Bash";
        if (next !== context) {
          context = next;
          editor.value = "";
          history = [];
          historyIndex = 0;
          savedDraft = "";
          pending = false;
          ticket = null;
          revision++;
          dismiss();
          delivery("Draft stays in this browser until Run.");
          controls();
        }
      },
      availability(connected, uncertain = false) {
        available = connected;
        if (uncertain && pending) delivery("Delivery uncertain. Reconnect to resume; do not run again.", "error");
        controls();
      },
      delivered(receipt) {
        if (ticket !== receipt) return;
        if (history.at(-1) !== receipt.text) history.push(receipt.text);
        if (history.length > 50) history.shift();
        historyIndex = history.length;
        pending = false;
        ticket = null;
        delivery(receipt.revision === revision
          ? "Delivered to shell. Output appears below; this is not a completion signal."
          : "Delivered. Your newer draft has not been sent.", "delivered");
        controls();
      }
    };
  }
  return {create, encode};
})();
