// Fills in this extension's ID on the instructions page (no inline scripts under MV3 CSP).
(function () {
  const id = chrome.runtime.id;
  document.getElementById("ext-id").textContent = id;
  const isEdge = /\bEdg\//.test(navigator.userAgent);
  document.getElementById("cmd-chrome").textContent =
    `powershell -ExecutionPolicy Bypass -File .\\native_host\\install_native_host.ps1 -ChromeExtensionId ${isEdge ? "<CHROME_ID>" : id}`;
  document.getElementById("cmd-edge").textContent =
    `powershell -ExecutionPolicy Bypass -File .\\native_host\\install_native_host.ps1 -EdgeExtensionId ${isEdge ? id : "<EDGE_ID>"}`;
})();
