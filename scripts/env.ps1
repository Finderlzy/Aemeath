# Aemeath environment setup for Windows PowerShell.
#
# Dot-source this before running server or check commands:
#     . .\scripts\env.ps1
#
# It pins the backend to the vendored upstream checkout and routes outbound
# traffic through the local proxy, which CLI tools do not inherit from the
# Windows system proxy setting.

$ErrorActionPreference = 'Stop'

$AemeathRoot = Split-Path -Parent $PSScriptRoot
$UpstreamDir = Join-Path $AemeathRoot 'vendor\Open-LLM-VTuber'
$VenvPython = Join-Path $UpstreamDir '.venv\Scripts\python.exe'

if (-not (Test-Path $VenvPython)) {
    throw "Backend virtualenv missing at $VenvPython. Run: uv sync --frozen --python 3.11 (in $UpstreamDir)"
}

# Outbound network: CLI tools ignore the Windows system proxy, so set it here.
# Override by setting AEMEATH_PROXY before dot-sourcing if a different port is used.
if (-not $env:AEMEATH_PROXY) { $env:AEMEATH_PROXY = 'http://127.0.0.1:7897' }
$env:HTTP_PROXY = $env:AEMEATH_PROXY
$env:HTTPS_PROXY = $env:AEMEATH_PROXY

# Keep model downloads inside the vendored checkout rather than the user profile.
$env:HF_HOME = Join-Path $UpstreamDir 'models'
$env:MODELSCOPE_CACHE = Join-Path $UpstreamDir 'models'

Set-Alias -Name aemeath-python -Value $VenvPython -Scope Global

Write-Host "Aemeath root : $AemeathRoot"
Write-Host "Backend venv : $VenvPython"
Write-Host "Proxy        : $env:HTTP_PROXY"
