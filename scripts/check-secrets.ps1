# Aemeath 提交前密钥扫描
#
# 用途
#     扫描**已纳入版本管理**的文件，发现疑似真实密钥即返回非零退出码。
#
# 背景
#     2026-09-14 曾因补丁生成时带出字面密钥，被 GitGuardian 告警
#     （见 docs/patches/README.md 的"密钥泄露事件与修复"）。本脚本用于
#     提交前拦截同类问题。
#
# 用法
#     .\scripts\check-secrets.ps1
#     if ($LASTEXITCODE -ne 0) { "存在疑似密钥，请先处理" }
#
# 退出码
#     0 = 未发现疑似密钥
#     1 = 发现疑似密钥（已列出文件与行号）

$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
Push-Location $repoRoot
try {
    # 只看受版本管理的文件；vendor/、data/、logs/ 等本就不入库。
    $tracked = git ls-files
    if ($LASTEXITCODE -ne 0) {
        Write-Error "git ls-files 失败，请确认在 Git 仓库内运行。"
    }

    # 只扫文本类文件，跳过二进制与图片。
    $textExt = @('.py', '.ps1', '.sh', '.md', '.yaml', '.yml', '.json',
                 '.toml', '.txt', '.patch', '.cfg', '.ini', '.ts', '.tsx',
                 '.js', '.jsx', '.env', '.example')

    # 每条规则：名称 + 正则 + 说明。
    # 注意：`${VAR}` 是本项目唯一允许的密钥形式，因此排除含 `$` 的赋值。
    $rules = @(
        @{ Name = 'OpenAI 风格密钥（sk-）'
           Pattern = 'sk-[A-Za-z0-9_-]{16,}'
           Hint = '真实密钥不得入库；改用 ${VAR} 形式。' },
        @{ Name = 'api_key 字面赋值'
           Pattern = "(?i)api[_-]?key\s*[:=]\s*['""][^'""`$][^'""]{6,}['""]"
           Hint = 'api_key 只能留空或写 ${VAR}。' },
        @{ Name = '私钥块'
           Pattern = '-----BEGIN [A-Z ]*PRIVATE KEY-----'
           Hint = '不得提交私钥。' },
        @{ Name = 'GitHub Token'
           Pattern = 'gh[pousr]_[A-Za-z0-9]{20,}'
           Hint = '不得提交 GitHub Token。' }
    )

    $findings = @()

    foreach ($file in $tracked) {
        $ext = [System.IO.Path]::GetExtension($file)
        if ($textExt -notcontains $ext) { continue }
        if (-not (Test-Path -LiteralPath $file)) { continue }

        $lines = Get-Content -LiteralPath $file -ErrorAction SilentlyContinue
        if ($null -eq $lines) { continue }

        for ($i = 0; $i -lt $lines.Count; $i++) {
            $line = $lines[$i]
            foreach ($rule in $rules) {
                if ($line -match $rule.Pattern) {
                    # 占位符与显而易见的假值不算命中。
                    if ($line -match 'xxxx|REDACTED|your[_-]?key|example|<.+>') { continue }
                    # 本机无鉴权服务的固定占位值，不是密钥。
                    if ($line -match 'lm-studio') { continue }
                    $findings += [pscustomobject]@{
                        File = $file
                        Line = $i + 1
                        Rule = $rule.Name
                        Hint = $rule.Hint
                        Text = $line.Trim()
                    }
                }
            }
        }
    }

    if ($findings.Count -eq 0) {
        Write-Host "check-secrets: PASS（已扫描 $($tracked.Count) 个受版本管理的文件）" -ForegroundColor Green
        exit 0
    }

    Write-Host "check-secrets: FAIL —— 发现 $($findings.Count) 处疑似密钥" -ForegroundColor Red
    foreach ($f in $findings) {
        Write-Host ""
        Write-Host "  $($f.File):$($f.Line)  [$($f.Rule)]" -ForegroundColor Yellow
        Write-Host "    $($f.Text)"
        Write-Host "    -> $($f.Hint)"
    }
    Write-Host ""
    Write-Host "已泄露的密钥必须吊销轮换，仅删除字符串不足以消除风险。" -ForegroundColor Red
    exit 1
}
finally {
    Pop-Location
}
