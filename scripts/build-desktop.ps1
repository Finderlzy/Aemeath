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

    # --- 2b. Build and deploy the management page -----------------------------
    #
    # The management window is a BrowserWindow that loads
    # http://127.0.0.1:12393/?page=manage — i.e. the WEB bundle served by the
    # backend, not the Electron renderer built above. Skipping this step leaves
    # the management window running whatever stale page is already deployed in
    # the backend's frontend/ directory.
    #
    # That is not a theoretical concern: V2-T03's "saved settings take effect"
    # path was dead on the real desktop because the deployed page predated the
    # code that notifies the main process. The Electron build alone cannot catch
    # it, because the page is served over HTTP.
    Write-Host 'Building and deploying the management page...'
    & cmd /c "npm.cmd run build:web"
    if ($LASTEXITCODE -ne 0) { throw "vite build:web failed (exit $LASTEXITCODE)" }

    $webDist = Join-Path $Web 'dist\web'
    $frontend = Join-Path $Root 'vendor\Open-LLM-VTuber\frontend'
    if (-not (Test-Path $frontend)) { throw "Backend frontend directory not found: $frontend" }

    # Remove the previous hashed bundles so stale files cannot accumulate and
    # shadow the current build via the old index.html.
    Get-ChildItem (Join-Path $frontend 'assets') -Filter 'main-*.js' -ErrorAction SilentlyContinue |
        Remove-Item -Force -ErrorAction SilentlyContinue
    Get-ChildItem (Join-Path $frontend 'assets') -Filter 'main-*.css' -ErrorAction SilentlyContinue |
        Remove-Item -Force -ErrorAction SilentlyContinue
    Copy-Item (Join-Path $webDist 'assets\*') (Join-Path $frontend 'assets') -Recurse -Force
    Copy-Item (Join-Path $webDist 'index.html') (Join-Path $frontend 'index.html') -Force

    # Verify the deployed page is the one just built, so a silent copy failure
    # cannot masquerade as a successful build.
    $deployedHtml = Get-Content (Join-Path $frontend 'index.html') -Raw
    $builtAssets = (Get-ChildItem (Join-Path $webDist 'assets') -Filter '*.js').Name
    $missing = @($builtAssets | Where-Object { $deployedHtml -notmatch [regex]::Escape($_) })
    if ($missing.Count -gt 0) {
        throw "Deployed index.html does not reference the freshly built asset(s): $($missing -join ', ')"
    }
    Write-Host "Management page deployed to: $frontend"

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
