# Aemeath desktop client build script.
#
# Usage
#     powershell -ExecutionPolicy Bypass -File .\scripts\build-desktop.ps1
#
# Output
#     vendor\Open-LLM-VTuber-Web\release\win-unpacked\Aemeath.exe
#
# Why this script exists instead of plain `npm run build:win`:
#
# 1. The Electron binary is not downloaded by default. The project has always
#    used ELECTRON_SKIP_BINARY_DOWNLOAD=1 for web-only builds, so
#    node_modules/electron/dist/ is empty. A desktop build must fetch it first.
#
# 2. `electron-builder --win` cannot finish on this machine. It unpacks the
#    winCodeSign toolchain, which stores macOS dylibs as symbolic links; Windows
#    refuses to create symlinks without Developer Mode or elevation, and the
#    build fails with "Cannot create symbolic link". That step prepares code
#    signing only - it is unrelated to this project's code, and no signature is
#    wanted here.
#
#    The script therefore assembles electron-builder's `--dir` layout by hand:
#    the Electron runtime plus resources/app (containing out/ and package.json).
#    That is exactly the directory structure electron-builder produces, and
#    electron.exe runs it directly.
#
# 3. @electron-toolkit/* must ship with the app. The main process keeps those
#    dependencies as require() calls (externalizeDepsPlugin does not bundle
#    them). Without them the main process throws at
#    require("@electron-toolkit/utils"), which shows up as **a live process with
#    no window ever appearing** - a failure mode that is very easy to mistake
#    for a successful build.

$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $PSScriptRoot
$Web = Join-Path $Root 'vendor\Open-LLM-VTuber-Web'
$Release = Join-Path $Web 'release\win-unpacked'

if (-not (Test-Path $Web)) { throw "Client source not found: $Web" }

# CLI tools do not inherit the Windows system proxy; Electron comes from a mirror.
if (-not $env:AEMEATH_PROXY) { $env:AEMEATH_PROXY = 'http://127.0.0.1:7897' }
$env:HTTP_PROXY = $env:AEMEATH_PROXY
$env:HTTPS_PROXY = $env:AEMEATH_PROXY
if (-not $env:ELECTRON_MIRROR) { $env:ELECTRON_MIRROR = 'https://npmmirror.com/mirrors/electron/' }

Push-Location $Web
try {
    # --- 1. Electron runtime -------------------------------------------------
    $electronExe = Join-Path $Web 'node_modules\electron\dist\electron.exe'
    if (-not (Test-Path $electronExe)) {
        Write-Host 'Downloading the Electron runtime...'
        # This variable makes npm skip the binary download; a desktop build must clear it.
        Remove-Item Env:\ELECTRON_SKIP_BINARY_DOWNLOAD -ErrorAction SilentlyContinue
        & cmd /c "npm.cmd rebuild electron" | Out-Null
        if (-not (Test-Path $electronExe)) {
            throw "Electron binary still missing: $electronExe (check proxy and ELECTRON_MIRROR)"
        }
    }
    Write-Host "Electron runtime: $electronExe"

    # --- 2. Compile main, preload and renderer --------------------------------
    Write-Host 'Compiling the three Electron processes...'
    & cmd /c "npm.cmd run build"
    if ($LASTEXITCODE -ne 0) { throw "electron-vite build failed (exit $LASTEXITCODE)" }

    # --- 3. Assemble the runnable directory -----------------------------------
    Write-Host "Assembling: $Release"
    Remove-Item $Release -Recurse -Force -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Force -Path $Release | Out-Null

    Copy-Item (Join-Path $Web 'node_modules\electron\dist\*') $Release -Recurse -Force

    $app = Join-Path $Release 'resources\app'
    New-Item -ItemType Directory -Force -Path $app | Out-Null
    Copy-Item (Join-Path $Web 'out') $app -Recurse -Force
    Copy-Item (Join-Path $Web 'package.json') $app -Force
    if (Test-Path (Join-Path $Web 'resources')) {
        Copy-Item (Join-Path $Web 'resources') $app -Recurse -Force
    }

    # Runtime dependencies the main bundle keeps as require() (see note 3 above).
    # '@' cannot start a single-quoted PowerShell string (it parses as the
    # splatting operator), so the scope name is assembled from a char code.
    $scopeName = [char]0x40 + 'electron-toolkit'
    $modules = Join-Path $app 'node_modules'
    New-Item -ItemType Directory -Force -Path $modules | Out-Null
    $scopeSource = Join-Path (Join-Path $Web 'node_modules') $scopeName
    if (Test-Path $scopeSource) {
        Copy-Item $scopeSource $modules -Recurse -Force
    }

    $exe = Join-Path $Release 'Aemeath.exe'
    Move-Item (Join-Path $Release 'electron.exe') $exe -Force

    # --- 4. Self-check --------------------------------------------------------
    if (-not (Test-Path $exe)) { throw "No executable produced: $exe" }
    if (-not (Test-Path (Join-Path $app 'out\main\index.js'))) { throw 'Main process entry missing' }
    $toolkitUtils = Join-Path (Join-Path $modules $scopeName) 'utils'
    if (-not (Test-Path $toolkitUtils)) {
        throw "$toolkitUtils was not shipped; the main process will throw at startup and no window will appear"
    }

    Write-Host ''
    Write-Host "Build complete: $exe"
    Write-Host 'Start the backend first: python scripts\run_server.py'
    Write-Host 'Acceptance probe: python scripts\probe_desktop_v2.py'
}
finally {
    Pop-Location
}
