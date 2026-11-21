<#
.SYNOPSIS
    Registers the My Download Manager native messaging host for Chrome and/or Edge
    for the current user (no administrator rights required).

.DESCRIPTION
    1. Validates the host executable and the extension ID(s).
    2. Copies the executable to the per-user install folder.
    3. Generates com.mydownloadmanager.native.json (the host manifest).
    4. Registers it under HKCU for each selected browser:
         HKCU\Software\Google\Chrome\NativeMessagingHosts\com.mydownloadmanager.native
         HKCU\Software\Microsoft\Edge\NativeMessagingHosts\com.mydownloadmanager.native
    A browser is registered only when its extension ID is supplied. Extension IDs
    already present in an existing manifest are kept, so Chrome and Edge can be
    registered in separate runs.

.PARAMETER ChromeExtensionId
    The 32-character ID shown on chrome://extensions for the unpacked extension.

.PARAMETER EdgeExtensionId
    The 32-character ID shown on edge://extensions for the unpacked extension.

.PARAMETER HostPath
    Path to MyDownloadManagerNativeHost.exe. Default: native_host\dist\MyDownloadManagerNativeHost.exe

.PARAMETER InstallDir
    Per-user install folder. Default: %LOCALAPPDATA%\MyDownloadManager\NativeHost

.PARAMETER AppPath
    Optional. Absolute path to an .exe that the popup's "Open My Download Manager"
    button may launch when the application is not running (for example
    ...\.venv\Scripts\pythonw.exe together with -AppArguments "C:\...\app.py").

.PARAMETER AppArguments
    Optional fixed arguments for -AppPath. Never supplied by the browser.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\native_host\install_native_host.ps1 -ChromeExtensionId abcdefghijklmnopabcdefghijklmnop

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\native_host\install_native_host.ps1 -ChromeExtensionId <CHROME_ID> -EdgeExtensionId <EDGE_ID>
#>
[CmdletBinding()]
param(
    [string]$ChromeExtensionId,
    [string]$EdgeExtensionId,
    [string]$HostPath,
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'MyDownloadManager\NativeHost'),
    [string]$AppPath,
    [string[]]$AppArguments = @()
)

$ErrorActionPreference = 'Stop'
$HostName = 'com.mydownloadmanager.native'
$ExeName = 'MyDownloadManagerNativeHost.exe'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$RegistryKeys = @{
    Chrome = "HKCU:\Software\Google\Chrome\NativeMessagingHosts\$HostName"
    Edge   = "HKCU:\Software\Microsoft\Edge\NativeMessagingHosts\$HostName"
}

function Fail([string]$Message) {
    Write-Host "ERROR: $Message" -ForegroundColor Red
    exit 1
}

function Test-ExtensionId([string]$Id) {
    # Chromium extension IDs are exactly 32 characters in the range a-p.
    return ($Id -cmatch '^[a-p]{32}$')
}

# ------------------------------------------------------------------ validation
if (-not $ChromeExtensionId -and -not $EdgeExtensionId) {
    Fail 'Supply -ChromeExtensionId and/or -EdgeExtensionId (copy the ID from chrome://extensions or edge://extensions).'
}
if ($ChromeExtensionId) {
    $ChromeExtensionId = $ChromeExtensionId.Trim()
    if (-not (Test-ExtensionId $ChromeExtensionId)) { Fail "Invalid Chrome extension ID '$ChromeExtensionId'. Expected 32 lowercase letters a-p." }
}
if ($EdgeExtensionId) {
    $EdgeExtensionId = $EdgeExtensionId.Trim()
    if (-not (Test-ExtensionId $EdgeExtensionId)) { Fail "Invalid Edge extension ID '$EdgeExtensionId'. Expected 32 lowercase letters a-p." }
}

if (-not $HostPath) { $HostPath = Join-Path $ScriptDir "dist\$ExeName" }
if (-not (Test-Path -LiteralPath $HostPath -PathType Leaf)) {
    Fail "Native host executable not found: $HostPath`nBuild it first: powershell -ExecutionPolicy Bypass -File `"$ScriptDir\build_host.ps1`""
}
$HostPath = (Resolve-Path -LiteralPath $HostPath).Path
if ([System.IO.Path]::GetExtension($HostPath) -ne '.exe') { Fail "Host path must be an .exe file: $HostPath" }

if ($AppPath) {
    if (-not [System.IO.Path]::IsPathRooted($AppPath)) { Fail "-AppPath must be an absolute path." }
    if (-not (Test-Path -LiteralPath $AppPath -PathType Leaf)) { Fail "-AppPath not found: $AppPath" }
    if ([System.IO.Path]::GetExtension($AppPath) -ne '.exe') { Fail "-AppPath must point to an .exe file." }
}

# --------------------------------------------------------------------- install
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
$InstallDir = (Resolve-Path -LiteralPath $InstallDir).Path
$InstalledExe = Join-Path $InstallDir $ExeName
$ManifestPath = Join-Path $InstallDir "$HostName.json"
$ConfigPath = Join-Path $InstallDir 'host_config.json'

if ($HostPath -ne $InstalledExe) {
    try {
        Copy-Item -LiteralPath $HostPath -Destination $InstalledExe -Force
    } catch {
        Fail "Could not copy the host to $InstalledExe. If a browser is using it, close the browser and retry. ($($_.Exception.Message))"
    }
}

# Allowed origins: keep IDs from an existing manifest, add the supplied ones.
$origins = New-Object System.Collections.Generic.List[string]
if (Test-Path -LiteralPath $ManifestPath) {
    try {
        $existing = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json
        foreach ($o in @($existing.allowed_origins)) {
            if ($o -cmatch '^chrome-extension://[a-p]{32}/$' -and -not $origins.Contains($o)) { $origins.Add($o) }
        }
    } catch { Write-Host 'Existing manifest was unreadable and will be replaced.' -ForegroundColor Yellow }
}
foreach ($id in @($ChromeExtensionId, $EdgeExtensionId)) {
    if ($id) {
        $o = "chrome-extension://$id/"
        if (-not $origins.Contains($o)) { $origins.Add($o) }
    }
}

$template = Get-Content -LiteralPath (Join-Path $ScriptDir 'manifest.template.json') -Raw | ConvertFrom-Json
$manifest = [ordered]@{
    name            = $template.name
    description     = $template.description
    path            = $InstalledExe
    type            = $template.type
    allowed_origins = $origins.ToArray()
}
if ($manifest.name -ne $HostName) { Fail 'manifest.template.json has an unexpected host name.' }
[System.IO.File]::WriteAllText($ManifestPath, ($manifest | ConvertTo-Json -Depth 4), $Utf8NoBom)

if ($AppPath) {
    $config = [ordered]@{ app_path = (Resolve-Path -LiteralPath $AppPath).Path; app_args = @($AppArguments) }
    [System.IO.File]::WriteAllText($ConfigPath, ($config | ConvertTo-Json -Depth 4), $Utf8NoBom)
}

# -------------------------------------------------------------------- registry
$results = @()
foreach ($browser in @('Chrome', 'Edge')) {
    $selected = if ($browser -eq 'Chrome') { [bool]$ChromeExtensionId } else { [bool]$EdgeExtensionId }
    if (-not $selected) { continue }
    $key = $RegistryKeys[$browser]
    try {
        New-Item -Path $key -Force | Out-Null
        Set-ItemProperty -Path $key -Name '(default)' -Value $ManifestPath
        $value = (Get-Item -Path $key).GetValue('')
        if ($value -ne $ManifestPath) { throw 'Registry value verification failed.' }
        $results += [pscustomobject]@{ Browser = $browser; Status = 'Registered'; Key = $key.Replace('HKCU:', 'HKCU') }
    } catch {
        $results += [pscustomobject]@{ Browser = $browser; Status = "FAILED: $($_.Exception.Message)"; Key = $key.Replace('HKCU:', 'HKCU') }
    }
}

# ------------------------------------------------------------------- summary
Write-Host ''
Write-Host 'My Download Manager - native messaging host installation' -ForegroundColor Cyan
Write-Host "  Host executable : $InstalledExe"
Write-Host "  Host manifest   : $ManifestPath"
Write-Host "  Allowed origins : $($origins -join ', ')"
if ($AppPath) { Write-Host "  Open-app target : $AppPath $($AppArguments -join ' ')" }
$results | Format-Table -AutoSize | Out-String | Write-Host

if ($results | Where-Object { $_.Status -ne 'Registered' }) {
    Write-Host 'One or more registrations failed.' -ForegroundColor Red
    exit 1
}
Write-Host 'Done. Restart the browser (close all of its windows) for the change to take effect.' -ForegroundColor Green
exit 0
