# Regenerate and verify the client patch series (docs/patches/web/).
#
# Usage
#     powershell -ExecutionPolicy Bypass -File .\scripts\regen-web-patch.ps1 [-Patch 0003]
#
# Why this is a script and not a couple of commands typed by hand
# --------------------------------------------------------------
#
# A web patch must be generated on a tree that already has the *earlier* patches
# applied. `0001` edits App.tsx (it adds the AemeathProvider import and mount),
# and `0002` layers the management pages on top. Generating on a pristine tree
# records those existing lines as additions, and the patch then always fails
# with "patch does not apply".
#
# The script encodes that rule and finishes by proving the result: it applies
# the whole series to a fresh checkout and compares every touched file byte for
# byte against the working copy. A patch that merely "applies" is not enough —
# this project has already shipped one that applied but produced different files.

param(
    [string]$Patch = '0003',
    [string]$RepoRoot = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = 'Stop'

$client = Join-Path $RepoRoot 'vendor\Open-LLM-VTuber-Web'
$patchDir = Join-Path $RepoRoot 'docs\patches\web'
$pinned = 'd176e7df2366952e3bacbf12cf9a8b18a4315932'

#: Patch order and the file each one is generated against.
$series = @('0001-aemeath-web-client.patch', '0002-aemeath-management-ui.patch', '0003-aemeath-desktop-tray-subtitle.patch')

$target = $series | Where-Object { $_ -like "$Patch*" } | Select-Object -First 1
if (-not $target) { throw "Unknown patch: $Patch (expected one of $($series -join ', '))" }

$targetIndex = [array]::IndexOf($series, $target)
$prerequisites = if ($targetIndex -gt 0) { $series[0..($targetIndex - 1)] } else { @() }

Write-Host "Regenerating $target"
Write-Host "  prerequisites: $(if ($prerequisites) { $prerequisites -join ', ' } else { '(none)' })"

# --- 1. Files the target patch owns -----------------------------------------
#
# Read from the current patch so the list cannot drift from what is already
# tracked: whatever the patch touches today is what it should touch tomorrow.
$existing = Join-Path $patchDir $target
if (Test-Path $existing) {
    $owned = Select-String -Path $existing -Pattern '^\+\+\+ b/' |
        ForEach-Object { $_.Line -replace '^\+\+\+ b/', '' }
    Write-Host "  files in the current patch: $($owned.Count)"
} else {
    throw "Patch not found: $existing. Create it once by hand to establish the file list."
}

# --- 2. Build the base tree --------------------------------------------------
$scratch = Join-Path $env:TEMP ("aemeath-regen-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
Write-Host "  scratch: $scratch"

& git clone --quiet --no-hardlinks $client $scratch
if ($LASTEXITCODE -ne 0) { throw 'git clone failed' }
Push-Location $scratch
try {
    & git checkout --quiet $pinned
    if ($LASTEXITCODE -ne 0) { throw "checkout $pinned failed" }

    foreach ($prereq in $prerequisites) {
        & git apply (Join-Path $patchDir $prereq)
        if ($LASTEXITCODE -ne 0) { throw "$prereq did not apply to the base tree" }
    }
    # Commit the base so the regenerated diff contains only the target's changes.
    & git add -A
    & git -c user.email=regen@local -c user.name=regen commit --quiet -m 'base'
    if ($LASTEXITCODE -ne 0) { throw 'failed to commit the base tree' }

    # --- 3. Overlay the working copy's versions of the owned files -----------
    foreach ($relative in $owned) {
        $source = Join-Path $client $relative
        $destination = Join-Path $scratch $relative
        if (-not (Test-Path $source)) { throw "working copy is missing $relative" }
        New-Item -ItemType Directory -Force -Path (Split-Path $destination) | Out-Null
        Copy-Item $source $destination -Force
    }

    & git add -A
    # cmd redirection keeps LF; PowerShell's Out-File writes CRLF and git apply
    # then reports "No valid patches in input".
    & cmd /c "git --no-pager diff --cached --no-color --binary > `"$existing`""
    if ($LASTEXITCODE -ne 0) { throw 'git diff failed' }
} finally {
    Pop-Location
}

$written = Get-Item $existing
Write-Host "  written: $($written.Length) bytes"

# --- 4. Prove it: apply the whole series to a fresh checkout ----------------
$verify = Join-Path $env:TEMP ("aemeath-verify-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
& git clone --quiet --no-hardlinks $client $verify
if ($LASTEXITCODE -ne 0) { throw 'git clone (verify) failed' }
Push-Location $verify
try {
    & git checkout --quiet $pinned
    foreach ($name in $series) {
        $path = Join-Path $patchDir $name
        & git apply --check $path
        if ($LASTEXITCODE -ne 0) { throw "$name failed --check on a clean tree" }
        & git apply $path
        if ($LASTEXITCODE -ne 0) { throw "$name failed to apply on a clean tree" }
    }
    Write-Host "  all $($series.Count) patches apply in order"

    $mismatches = 0
    foreach ($relative in $owned) {
        $a = (Get-FileHash (Join-Path $verify $relative) -Algorithm SHA256).Hash
        $b = (Get-FileHash (Join-Path $client $relative) -Algorithm SHA256).Hash
        if ($a -ne $b) { Write-Host "  DIFFERS: $relative" -ForegroundColor Red; $mismatches++ }
    }
    if ($mismatches -gt 0) { throw "$mismatches file(s) differ from the working copy" }
    Write-Host "  all $($owned.Count) files byte-identical to the working copy" -ForegroundColor Green
} finally {
    Pop-Location
    Remove-Item $verify -Recurse -Force -ErrorAction SilentlyContinue
}

Remove-Item $scratch -Recurse -Force -ErrorAction SilentlyContinue
Write-Host "`n$target regenerated and verified." -ForegroundColor Green
