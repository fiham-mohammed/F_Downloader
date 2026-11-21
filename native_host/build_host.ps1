<#
.SYNOPSIS
    Builds MyDownloadManagerNativeHost.exe with PyInstaller and verifies it.

.DESCRIPTION
    PyInstaller is a build-time dependency only (native_host\requirements.txt).
    The host is standard-library Python; PySide6, httpx and other application
    dependencies are explicitly excluded to keep the executable small.

    Output: native_host\dist\MyDownloadManagerNativeHost.exe

.PARAMETER Python
    Python interpreter to use. Defaults to the project's .venv, then "python".

.PARAMETER SkipVerify
    Skip the end-to-end verification of the built executable.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\native_host\build_host.ps1
#>
[CmdletBinding()]
param(
    [string]$Python,
    [switch]$SkipVerify
)

$ErrorActionPreference = 'Stop'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $ScriptDir
$ExeName = 'MyDownloadManagerNativeHost'
$DistDir = Join-Path $ScriptDir 'dist'
$WorkDir = Join-Path $ScriptDir 'build'

if (-not $Python) {
    $venvPython = Join-Path $Root '.venv\Scripts\python.exe'
    if (Test-Path $venvPython) { $Python = $venvPython } else { $Python = 'python' }
}

Write-Host "Using Python: $Python" -ForegroundColor Cyan
& $Python --version
if ($LASTEXITCODE -ne 0) { throw "Python was not found. Pass -Python <path to python.exe>." }

Write-Host 'Installing build requirements (PyInstaller)...' -ForegroundColor Cyan
& $Python -m pip install --disable-pip-version-check -q -r (Join-Path $ScriptDir 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'pip install failed.' }

Write-Host 'Building native host executable...' -ForegroundColor Cyan
$pyiArgs = @(
    '-m', 'PyInstaller',
    '--noconfirm', '--clean', '--onefile', '--console',
    '--name', $ExeName,
    '--paths', $Root,
    '--distpath', $DistDir,
    '--workpath', $WorkDir,
    '--specpath', $WorkDir,
    '--hidden-import', 'mdm.browser_integration.ipc_protocol',
    '--hidden-import', 'mdm.browser_integration.request_validator',
    '--exclude-module', 'PySide6',
    '--exclude-module', 'shiboken6',
    '--exclude-module', 'httpx',
    '--exclude-module', 'platformdirs',
    '--exclude-module', 'tkinter',
    '--exclude-module', 'unittest',
    (Join-Path $ScriptDir 'host.py')
)
& $Python @pyiArgs
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed.' }

$Exe = Join-Path $DistDir "$ExeName.exe"
if (-not (Test-Path $Exe -PathType Leaf)) { throw "Build finished but $Exe was not produced." }
$sizeMb = [math]::Round((Get-Item $Exe).Length / 1MB, 2)
Write-Host "Built: $Exe ($sizeMb MB)" -ForegroundColor Green

if (-not $SkipVerify) {
    Write-Host 'Verifying executable (framing, IPC forwarding, responses, clean exit)...' -ForegroundColor Cyan
    & $Python (Join-Path $ScriptDir 'verify_host.py') $Exe
    if ($LASTEXITCODE -ne 0) { throw 'Executable verification FAILED.' }
}

Write-Host ''
Write-Host 'Next step - register the host for your browser(s):' -ForegroundColor Yellow
Write-Host "  powershell -ExecutionPolicy Bypass -File `"$ScriptDir\install_native_host.ps1`" -ChromeExtensionId <ID> -EdgeExtensionId <ID>"
