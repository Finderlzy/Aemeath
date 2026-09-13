<#
.SYNOPSIS
    停止本地 GPT-SoVITS 服务 (默认端口 9880)
#>
param(
    [int]$Port = 9880
)

$conns = Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue
if (-not $conns) {
    Write-Host ("No GPT-SoVITS process found listening on port {0}." -f $Port)
    exit 0
}

$pids = $conns | Select-Object -ExpandProperty OwningProcess -Unique
foreach ($p in $pids) {
    if ($p -gt 0) {
        Write-Host ("Stopping process PID: {0}..." -f $p)
        Stop-Process -Id $p -Force -ErrorAction SilentlyContinue
    }
}

Start-Sleep -Seconds 1
$check = Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue
if (-not $check) {
    Write-Host ("GPT-SoVITS on port {0} stopped." -f $Port)
} else {
    Write-Warning ("Port {0} still has active connections." -f $Port)
}