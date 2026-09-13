<#
.SYNOPSIS
    启动本地 GPT-SoVITS api_v2 服务 (默认端口 9880)
#>
param(
    [int]$Port = 9880,
    [string]$ServiceDir = ""
)

if (-not $ServiceDir) {
    if ($env:GPT_SOVITS_DIR) {
        $ServiceDir = $env:GPT_SOVITS_DIR
    } else {
        $ServiceDir = "E:\WorkSpace\Tools\GPT-SoVITS"
    }
}

# 1. 如果端口已有连接，严密验证其 HTTP 接口真实就绪状态，而非仅仅检查端口
$conn = Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue
if ($conn) {
    try {
        $resp = Invoke-WebRequest -Uri ("http://127.0.0.1:{0}/openapi.json" -f $Port) -TimeoutSec 3 -ErrorAction Stop
        if ($resp.StatusCode -eq 200) {
            Write-Host ("GPT-SoVITS api_v2 is already running and ready on port {0} (PID: {1})" -f $Port, $conn[0].OwningProcess)
            exit 0
        }
    } catch {
        Write-Error ("Port {0} is occupied by PID {1} but HTTP endpoint /openapi.json is not healthy." -f $Port, $conn[0].OwningProcess)
        exit 1
    }
}

$pythonExe = Join-Path $ServiceDir ".venv\Scripts\python.exe"
if (-not (Test-Path $pythonExe)) {
    $pythonExe = Join-Path $ServiceDir "runtime\python.exe"
}
if (-not (Test-Path $pythonExe)) {
    Write-Error ("GPT-SoVITS python interpreter not found in: {0}" -f $ServiceDir)
    exit 1
}

Write-Host ("Starting GPT-SoVITS api_v2 on port {0} from {1}..." -f $Port, $ServiceDir)
$pinfo = New-Object System.Diagnostics.ProcessStartInfo
$pinfo.FileName = $pythonExe
$pinfo.Arguments = ("api_v2.py -a 127.0.0.1 -p {0} -c GPT_SoVITS/configs/tts_infer.yaml" -f $Port)
$pinfo.WorkingDirectory = $ServiceDir
$pinfo.UseShellExecute = $false
$pinfo.CreateNoWindow = $true

$proc = [System.Diagnostics.Process]::Start($pinfo)
Write-Host ("Process started with PID: {0}. Waiting for service readiness..." -f $proc.Id)

for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 1
    try {
        $resp = Invoke-WebRequest -Uri ("http://127.0.0.1:{0}/openapi.json" -f $Port) -TimeoutSec 2 -ErrorAction Stop
        if ($resp.StatusCode -eq 200) {
            Write-Host ("GPT-SoVITS api_v2 ready at http://127.0.0.1:{0}" -f $Port)
            exit 0
        }
    } catch {
        # continue waiting
    }
}

Write-Error ("Timeout waiting for GPT-SoVITS on port {0} to become ready." -f $Port)
exit 1
