// My Download Manager - popup controller (Stage 6A).
import { validateDownloadUrl, detectBrowser, browserLabel } from "./lib/validation.js";
import { CAPTURE_PERMISSIONS, CAPTURE_MODE_LABELS } from "./lib/capture_rules.js";

const $ = (id) => document.getElementById(id);

const STATUS_TEXT = {
  checking: { label: "Checking connection…", dot: "dot-checking", short: "Checking…" },
  connected: { label: "Connected", dot: "dot-ok", short: "Connected" },
  app_not_running: { label: "Application not running", dot: "dot-warn", short: "Application not running" },
  native_host_unavailable: { label: "Native host unavailable", dot: "dot-error", short: "Native host unavailable" },
  connection_error: { label: "Connection error", dot: "dot-error", short: "Connection error" },
};

const STATUS_HINT = {
  connected: "Ready to receive downloads.",
  app_not_running: "Start My Download Manager on this PC.",
};

function send(type, extra = {}) {
  return chrome.runtime.sendMessage({ type, ...extra });
}

function showMessage(text, kind) {
  const el = $("add-message");
  el.textContent = text;
  el.className = `message message-${kind}`;
  el.hidden = !text;
}

function renderStatus(info) {
  const key = STATUS_TEXT[info.status] ? info.status : "connection_error";
  const meta = STATUS_TEXT[key];
  $("status-dot").className = `dot ${meta.dot}`;
  $("status-label").textContent = meta.label;
  $("status-detail").textContent = STATUS_HINT[key] || info.message || "";
  $("info-status").textContent = meta.short;
  $("info-browser").textContent = info.browser_label || browserLabel(detectBrowser());
  const parts = [`Extension ${info.extension_version || chrome.runtime.getManifest().version}`];
  if (info.app_version) parts.push(`App ${info.app_version}`);
  $("info-version").textContent = parts.join(" · ");
  $("open-app-btn").disabled = !(key === "connected" || info.open_app_supported);
  $("open-app-btn").title = $("open-app-btn").disabled
    ? "Available when the native host is installed and the application is running or configured for launch."
    : "";
}

async function refreshStatus() {
  renderStatus({ status: "checking", extension_version: chrome.runtime.getManifest().version });
  $("retry-btn").disabled = true;
  try {
    renderStatus(await send("getStatus"));
  } catch (_e) {
    renderStatus({ status: "connection_error", message: "The extension background service is not responding." });
  } finally {
    $("retry-btn").disabled = false;
  }
}

function formatTime(ms) {
  try {
    return new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  } catch (_e) {
    return "";
  }
}

async function refreshPending() {
  const { pending = [] } = (await send("listPending")) || {};
  const list = $("pending-list");
  list.replaceChildren();
  $("pending-card").hidden = pending.length === 0;
  for (const item of pending.slice().reverse()) {
    const li = document.createElement("li");
    const text = document.createElement("div");
    text.className = "pending-text";
    const url = document.createElement("span");
    url.className = "pending-url";
    url.textContent = item.message.url; // rendered as plain text, never parsed as HTML
    url.title = item.message.url;
    const err = document.createElement("span");
    err.className = "muted small";
    err.textContent = `${formatTime(item.failed_at)} · ${item.error}`;
    text.append(url, err);

    const retry = document.createElement("button");
    retry.className = "btn btn-secondary btn-sm";
    retry.type = "button";
    retry.textContent = "Retry";
    retry.addEventListener("click", async () => {
      retry.disabled = true;
      const res = await send("retryPending", { requestId: item.message.request_id });
      showMessage(res.ok ? (res.message || "Download added.") : (res.message || "Still unavailable."),
        res.ok ? "success" : "error");
      await Promise.all([refreshPending(), refreshStatus()]);
    });
    const dismiss = document.createElement("button");
    dismiss.className = "btn btn-ghost btn-sm";
    dismiss.type = "button";
    dismiss.textContent = "Dismiss";
    dismiss.addEventListener("click", async () => {
      await send("dismissPending", { requestId: item.message.request_id });
      await refreshPending();
    });
    const buttons = document.createElement("div");
    buttons.className = "pending-buttons";
    buttons.append(retry, dismiss);
    li.append(text, buttons);
    list.append(li);
  }
}

$("add-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const check = validateDownloadUrl($("url-input").value);
  if (!check.ok) {
    showMessage(check.message, "error");
    $("url-input").focus();
    return;
  }
  $("add-btn").disabled = true;
  showMessage("Sending to My Download Manager…", "info");
  try {
    const res = await send("addUrl", { url: check.url });
    if (res.ok) {
      const name = res.download && res.download.filename;
      showMessage(res.status === "duplicate" ? "This link was already sent."
        : `Added${name ? `: ${name}` : " to My Download Manager."}`, "success");
      $("url-input").value = "";
    } else {
      showMessage(res.message || "The download could not be added.", "error");
    }
    if (res.connection_status) renderStatus({ ...(await send("getStatus")) });
  } catch (_e) {
    showMessage("The extension background service is not responding.", "error");
  } finally {
    $("add-btn").disabled = false;
    refreshPending();
  }
});

$("url-input").addEventListener("input", () => showMessage("", "info"));
$("retry-btn").addEventListener("click", () => { refreshStatus(); refreshPending(); });

$("open-app-btn").addEventListener("click", async () => {
  $("open-app-btn").disabled = true;
  const res = await send("openApp");
  showMessage(res.ok ? (res.message || "My Download Manager opened.") : (res.message || "Could not open the application."),
    res.ok ? "success" : "error");
  setTimeout(refreshStatus, res.status === "launched" ? 2500 : 300);
});

$("help-btn").addEventListener("click", () => {
  chrome.tabs.create({ url: chrome.runtime.getURL("instructions.html") });
  window.close();
});

chrome.action.setBadgeText({ text: "" });
refreshStatus();
refreshPending();

// ------------------------------------------------- Stage 6B: automatic capture
function showCaptureMessage(text, kind) {
  const el = $("capture-message");
  el.textContent = text;
  el.className = `message message-${kind}`;
  el.hidden = !text;
}

function fallbackItem(label, detail, buttonText, onClick) {
  const li = document.createElement("li");
  const text = document.createElement("div");
  text.className = "pending-text";
  const name = document.createElement("span");
  name.className = "pending-url";
  name.textContent = label;
  name.title = label;
  const why = document.createElement("span");
  why.className = "muted small";
  why.textContent = detail;
  text.append(name, why);
  const button = document.createElement("button");
  button.className = "btn btn-secondary btn-sm";
  button.type = "button";
  button.textContent = buttonText;
  button.addEventListener("click", async () => {
    button.disabled = true;
    await onClick();
  });
  li.append(text, button);
  return li;
}

/** One line per decision; everything is rendered as plain text. */
export function describeDecision(d) {
  const what = d.filename || "download";
  const outcome = d.decision === "committed" ? "sent to My Download Manager"
    : d.decision === "browser_fallback" ? "restarted in the browser"
      : `left in the browser (${d.reason || d.decision})`;
  const facts = [d.method || "method ?", d.resource_type, d.mime, d.initiator !== "browser" ? d.initiator : null]
    .filter(Boolean).join(", ");
  return `${what}: ${outcome} [${facts}]`;
}

function renderDiagnostics(list) {
  $("diag-details").hidden = list.length === 0;
  $("diag-latest").textContent = list.length ? describeDecision(list[0]) : "—";
  const ul = $("diag-list");
  ul.replaceChildren();
  for (const d of list) {
    const li = document.createElement("li");
    li.textContent = `${formatTime(d.at)} · ${describeDecision(d)}`;
    ul.append(li);
  }
}

async function refreshCapture() {
  let st = null;
  try {
    st = await send("getCaptureState");
  } catch (_e) {
    st = null;
  }
  const select = $("capture-mode");
  if (!st) {
    $("capture-status").textContent = "The extension background service is not responding.";
    select.disabled = true;
    return;
  }
  select.value = st.mode;
  select.disabled = !st.ok;
  const label = CAPTURE_MODE_LABELS[st.mode] || "Off";
  $("info-capture").textContent = st.ok ? label : "Unavailable";
  $("info-pending").textContent = String(st.captures.length);
  let status;
  if (!st.ok) {
    status = `${st.message} Automatic capture is inactive, so downloads stay in the browser.`;
  } else if (st.mode === "disabled") {
    status = "Off. Downloads stay in the browser; the right-click menu still works.";
  } else if (!st.permissions) {
    status = `${label} is selected in My Download Manager, but this browser has not granted the capture `
      + "permissions. Choose the mode again to grant them.";
  } else {
    status = `${label}. Selected file types: ${st.extensions.length}. Excluded websites: ${st.excluded_sites.length}.`;
  }
  $("capture-status").textContent = status;
  $("capture-permission-note").hidden = st.permissions;
  renderDiagnostics(Array.isArray(st.diagnostics) ? st.diagnostics : []);

  const list = $("fallback-list");
  list.replaceChildren();
  for (const f of st.fallbacks) {
    list.append(fallbackItem(f.filename || "Download", "Needs your browser session (sign-in/cookies).",
      "Download in browser", async () => {
        const r = await send("claimFallback", { requestId: f.request_id });
        showCaptureMessage(r.ok ? "Downloading in the browser." : (r.message || "Could not hand it back."),
          r.ok ? "success" : "error");
        refreshCapture();
      }));
  }
  for (const c of st.captures.filter((x) => x.phase === "fallback_failed")) {
    list.append(fallbackItem(c.filename || "Download", "Could not be restarted in the browser.",
      "Retry in browser", async () => {
        const r = await send("retryCaptureFallback", { requestId: c.request_id });
        showCaptureMessage(r.ok ? "Downloading in the browser." : "Still failing; try again later.",
          r.ok ? "success" : "error");
        refreshCapture();
      }));
  }
}

$("capture-mode").addEventListener("change", async () => {
  const mode = $("capture-mode").value;
  if (mode !== "disabled") {
    // Must be the first call in the user's gesture: the browser shows its own prompt.
    let granted = false;
    try {
      granted = await chrome.permissions.request(CAPTURE_PERMISSIONS);
    } catch (_e) {
      granted = false;
    }
    if (!granted) {
      showCaptureMessage("Permission was not granted, so automatic capture stays off. "
        + "Downloads continue normally in the browser.", "error");
      await refreshCapture();
      return;
    }
  }
  showCaptureMessage("Saving…", "info");
  const r = await send("setCaptureMode", { mode });
  if (r && r.ok) {
    if (mode === "disabled") {
      try {
        await chrome.permissions.remove(CAPTURE_PERMISSIONS);
      } catch (_e) {
        // already removed
      }
    }
    showCaptureMessage(mode === "disabled" ? "Automatic capture turned off."
      : `Automatic capture: ${CAPTURE_MODE_LABELS[mode]}.`, "success");
  } else {
    showCaptureMessage((r && r.message) || "My Download Manager is not reachable; the setting was not changed.", "error");
  }
  refreshCapture();
});

$("settings-btn").addEventListener("click", async () => {
  const r = await send("openSettings");
  showCaptureMessage(r && r.ok ? "Opened in My Download Manager." : ((r && r.message) || "Could not open the app."),
    r && r.ok ? "success" : "error");
});

refreshCapture();
