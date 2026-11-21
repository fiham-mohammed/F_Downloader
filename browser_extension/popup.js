// My Download Manager - popup controller (Stage 6A).
import { validateDownloadUrl, detectBrowser, browserLabel } from "./lib/validation.js";

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
