<#
.SYNOPSIS
    Removes the My Download Manager native messaging host registration.

.DESCRIPTION
    Removes ONLY what install_native_host.ps1 created:
      * HKCU\Software\Google\Chrome\NativeMessagingHosts\com.mydownloadmanager.native
      * HKCU\Software\Microsoft\Edge\NativeMessagingHosts\com.mydownloadmanager.native
      * The host executable, manifest and host_config.json in the install folder
    A registry key is removed only if it points to a manifest for
    com.mydownloadmanager.native. Downloads, the download database, settings
    and logs are never touched.

.PARAMETER Browser
    Chrome, Edge or Both (default).

.PARAMETER InstallDir
    Default: %LOCALAPPDATA%\MyDownloadManager\NativeHost

.PARAMETER KeepFiles
    Only remove registry entries; leave the installed host files.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\native_host\uninstall_native_host.ps1
#>
[CmdletBinding()]
param(
    [ValidateSet('Chrome', 'Edge', 'Both')]
    [string]$Browser = 'Both',
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'MyDownloadManager\NativeHost'),
    [switch]$KeepFiles
)

$ErrorActionPreference = 'Stop'
$HostName = 'com.mydownloadmanager.native'
$RegistryKeys = [ordered]@{
    Chrome = "HKCU:\Software\Google\Chrome\NativeMessagingHosts\$HostName"
    Edge   = "HKCU:\Software\Microsoft\Edge\NativeMessagingHosts\$HostName"
}

function Test-OurManifest([string]$Path) {
    if (-not $Path) { return $false }
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        # Manifest already deleted: the key name itself identifies our host.
        return ([System.IO.Path]::GetFileName($Path) -eq "$HostName.json")
    }
    try {
        return ((Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json).name -eq $HostName)
    } catch { return $false }
}

$results = @()
foreach ($name in $RegistryKeys.Keys) {
    if ($Browser -ne 'Both' -and $Browser -ne $name) { continue }
    $key = $RegistryKeys[$name]
    if (-not (Test-Path -Path $key)) {
        $results += [pscustomobject]@{ Browser = $name; Status = 'Not registered' }
        continue
    }
    $value = (Get-Item -Path $key).GetValue('')
    if (-not (Test-OurManifest $value)) {
        $results += [pscustomobject]@{ Browser = $name; Status = "Skipped: key points to '$value', not a My Download Manager manifest" }
        continue
    }
    Remove-Item -Path $key -Recurse -Force
    $results += [pscustomobject]@{ Browser = $name; Status = 'Removed' }
}

$filesRemoved = @()
$stillRegistered = @($RegistryKeys.Values | Where-Object { Test-Path -Path $_ })
if (-not $KeepFiles -and $stillRegistered.Count -eq 0 -and (Test-Path -LiteralPath $InstallDir)) {
    foreach ($file in @('MyDownloadManagerNativeHost.exe', "$HostName.json", 'host_config.json')) {
        $p = Join-Path $InstallDir $file
        if (Test-Path -LiteralPath $p -PathType Leaf) {
            try {
                Remove-Item -LiteralPath $p -Force
                $filesRemoved += $p
            } catch {
                Write-Host "Could not remove $p (close the browser and retry): $($_.Exception.Message)" -ForegroundColor Yellow
            }
        }
    }
    if (-not (Get-ChildItem -LiteralPath $InstallDir -Force | Select-Object -First 1)) {
        Remove-Item -LiteralPath $InstallDir -Force
    }
} elseif ($stillRegistered.Count -gt 0 -and -not $KeepFiles) {
    Write-Host 'Host files kept because another browser is still registered.' -ForegroundColor Yellow
}

Write-Host ''
Write-Host 'My Download Manager - native messaging host removal' -ForegroundColor Cyan
$results | Format-Table -AutoSize | Out-String | Write-Host
foreach ($f in $filesRemoved) { Write-Host "  Deleted $f" }
Write-Host 'Downloads, settings and the download database were not modified.' -ForegroundColor Green
exit 0
