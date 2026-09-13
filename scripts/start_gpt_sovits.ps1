<#
.SYNOPSIS
    启动本地 GPT-SoVITS api_v2 服务 (默认端口 9880)
#>
param(
    [int]$Port = 9880
)

$serviceDir = "E:\WorkSpace\Tools\GPT-SoVITS"
$pythonExe = Join-Path $serviceDir ".venv\Scripts\python.exe"

$conn = Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue
if ($conn) {
    Write-Host ("GPT-SoVITS is already running on port {0} (PID: {1})" -f $Port, $conn[0].OwningProcess)
    exit 0
}

if (-not (Test-Path $pythonExe)) {
    Write-Error ("GPT-SoVITS python interpreter not found: {0}" -f $pythonExe)
    exit 1
}

Write-Host ("Starting GPT-SoVITS api_v2 on port {0}..." -f $Port)
$pinfo = New-Object System.Diagnostics.ProcessStartInfo
$pinfo.FileName = $pythonExe
$pinfo.Arguments = ("api_v2.py -a 127.0.0.1 -p {0} -c GPT_SoVITS/configs/tts_infer.yaml" -f $Port)
$pinfo.WorkingDirectory = $serviceDir
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

Write-Warning ("Timeout waiting for port {0} to be ready. Service may still be initializing." -f $Port)
exit 0