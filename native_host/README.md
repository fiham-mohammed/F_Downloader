# My Download Manager – Native Messaging Host

Host name: **`com.mydownloadmanager.native`** · Executable: **`MyDownloadManagerNativeHost.exe`**

This is the bridge between the Chrome/Edge extension (`../browser_extension`) and the running My Download Manager application. It uses the Python standard library only. It shares `mdm/browser_integration/ipc_protocol.py` and `request_validator.py` with the application, so the validation rules are identical on both sides.

## Files

| File | Purpose |
|---|---|
| `host.py` | The host: native-messaging framing, validation, authenticated IPC forwarding |
| `manifest.template.json` | Template for the host manifest that the installer generates |
| `requirements.txt` | **Build-only** dependency (PyInstaller). Not needed to run the app. |
| `build_host.ps1` | Builds `dist\MyDownloadManagerNativeHost.exe` and verifies it |
| `install_native_host.ps1` | Per-user (HKCU) registration for Chrome and/or Edge |
| `uninstall_native_host.ps1` | Removes only this host's registrations and installed files |
| `verify_host.py` | End-to-end check of `host.py` or the built `.exe` against a mock application |

## Quick start (PowerShell, project root)

```powershell
# 1. Build (installs PyInstaller into the active/.venv Python, builds, verifies)
powershell -ExecutionPolicy Bypass -File .\native_host\build_host.ps1

# 2. Register (IDs come from chrome://extensions and edge://extensions after "Load unpacked")
powershell -ExecutionPolicy Bypass -File .\native_host\install_native_host.ps1 -ChromeExtensionId <CHROME_ID> -EdgeExtensionId <EDGE_ID>

# 3. Restart the browser completely, then start My Download Manager.

# Remove again
powershell -ExecutionPolicy Bypass -File .\native_host\uninstall_native_host.ps1
```

## Protocol

**Browser ↔ host** (Chromium Native Messaging): each message is a 4-byte **little-endian** unsigned length followed by that many bytes of UTF-8 JSON.

* The maximum message size is 1 MB in each direction.
* Partial pipe reads are handled, and many messages can be processed in one host process.
* stdout is flushed after every response. Nothing else is ever written to stdout; logs go to `%LOCALAPPDATA%\MyDownloadManager\logs\native_host.log`.

| Action | Request | Response |
|---|---|---|
| `ping` | `{"action":"ping"}` | `{"ok":true,"status":"connected","app_version":"0.6.0",...}` |
| `add_download` | `{"action":"add_download","request_id":"…","url":"https://…","filename":null,"referrer":"https://…","page_title":"…","browser":"chrome"\|"edge"}` | `{"ok":true,"status":"accepted"\|"duplicate","download":{"id":"…","filename":"…"}}` |
| `open_app` | `{"action":"open_app"}` | `{"ok":true,"status":"ok"\|"launched"}` or `open_not_supported` |

Errors have the form `{"ok":false,"status":"error","error_code":"…","message":"…"}`. The codes are:

* **framing and schema:** `invalid_frame`, `message_too_large`, `invalid_json`, `invalid_message`, `unsupported_action`, `missing_field`, `invalid_field`
* **URL checks:** `invalid_url`, `unsupported_scheme`, `embedded_credentials`
* **reaching the application:** `app_not_running`, `invalid_endpoint`, `timeout`, `connection_lost`, `connection_failed`, `unauthorized`, `shutting_down`
* **application-side:** `queue_rejected`, `internal_error`, `storage_error` (retryable: the request could not be recorded durably) and `request_id_conflict` (permanent: the ID was already used for a different URL)

**Host ↔ application:** the same length-prefixed framing over TCP to `127.0.0.1`.

* The port and a per-launch token are read from `%LOCALAPPDATA%\MyDownloadManager\browser_integration\endpoint.json`. The host always connects to 127.0.0.1 and never reads a host address from the file.
* Request envelope: `{"v":1,"token","request_id","ts","action","payload"}`.
* See `mdm/browser_integration/ipc_protocol.py` for the full specification.

## Security

* **Cookies vs. signed URLs:** the extension never reads browser cookies, saved passwords or `Authorization` headers, so none are ever sent through this host. The link URL itself **is** forwarded unchanged, including its query string. A signed download link (for example `?X-Amz-Signature=…&token=…`) carries its access token in that query string and would stop working if it were stripped. Such URLs are treated as sensitive: this host logs only `scheme://host/…` and never writes the URL anywhere else.
* Nothing from the browser is executed or evaluated, or used as a path or a command line.
* Only `http`/`https` URLs without embedded credentials are forwarded. Filenames must be bare names.
* `open_app` launches only the `.exe` written to `host_config.json` by `install_native_host.ps1 -AppPath …`, with `shell=False`.
* The authentication token is never logged, and URLs are logged as `scheme://host/…` only.

See the main [README](../README.md#browser-integration-stage-6a) for the full setup, testing guide, troubleshooting and limitations.
