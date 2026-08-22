param(
    [switch]$Force,
    [switch]$Rollback
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$RepoOwner = "Matthew81975"
$RepoName = "Homestead_AI"
$Branch = "main"
$Root = $PSScriptRoot
$UpdateRoot = Join-Path $Root ".hcs-update"
$StatePath = Join-Path $UpdateRoot "state.json"
$Headers = @{ "User-Agent" = "HCS-AI-Updater"; "Accept" = "application/vnd.github+json" }

function Write-State([hashtable]$state) {
    New-Item -ItemType Directory -Force -Path $UpdateRoot | Out-Null
    $state | ConvertTo-Json -Depth 8 | Set-Content -Encoding UTF8 -Path $StatePath
}

function Read-State {
    if (-not (Test-Path $StatePath)) { return @{} }
    try {
        $obj = Get-Content $StatePath -Raw | ConvertFrom-Json
        $state = @{}
        $obj.PSObject.Properties | ForEach-Object { $state[$_.Name] = $_.Value }
        return $state
    } catch {
        return @{}
    }
}

function Ensure-Parent([string]$path) {
    $parent = Split-Path -Parent $path
    if ($parent) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
}

function Assert-SafeRelativePath([string]$path) {
    if ([string]::IsNullOrWhiteSpace($path)) { throw "Update manifest contained an empty path." }
    $p = $path.Replace("\", "/")
    while ($p.StartsWith("./")) { $p = $p.Substring(2) }
    if ($p -match "^[A-Za-z]:" -or $p.StartsWith("/") -or $p -match "(^|/)\.\.(/|$)") {
        throw "Unsafe update path: $path"
    }

    # Machine-local state and maintenance/bootstrap scripts are intentionally
    # outside routine application updates. Maintenance scripts can be refreshed
    # manually from the repository when needed.
    $protectedExact = @(
        "config.json", ".env", "secrets.json",
        ".git", ".github", ".venv", ".hcs-update", "data", "models", "runtime",
        "update_hcs.ps1", "install.ps1", "install.bat", "setup_internal_ai.ps1",
        "BOOTSTRAP_SELF_UPDATE.bat"
    )
    $protectedPrefixes = @(".git/", ".github/", ".venv/", ".hcs-update/", "data/", "models/", "runtime/")
    if ($protectedExact -contains $p) { throw "Update manifest attempted to replace protected local/maintenance file: $p" }
    foreach ($prefix in $protectedPrefixes) {
        if ($p.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Update manifest attempted to replace protected local path: $p"
        }
    }
    return $p
}

function Get-RawUrl([string]$sha, [string]$path) {
    $encoded = (($path -split "/") | ForEach-Object { [uri]::EscapeDataString($_) }) -join "/"
    return "https://raw.githubusercontent.com/$RepoOwner/$RepoName/$sha/$encoded"
}

function Restore-Backup([string]$backupDir) {
    $manifestPath = Join-Path $backupDir "manifest.json"
    if (-not (Test-Path $manifestPath)) { throw "Rollback manifest not found: $manifestPath" }
    $manifest = Get-Content $manifestPath -Raw | ConvertFrom-Json

    foreach ($entry in $manifest.entries) {
        $relative = [string]$entry.path
        $target = Join-Path $Root ($relative -replace "/", "\")
        if (Test-Path $target) { Remove-Item $target -Recurse -Force }
        if ($entry.existed) {
            $saved = Join-Path $backupDir ($relative -replace "/", "\")
            Ensure-Parent $target
            if (Test-Path $saved) { Copy-Item $saved $target -Recurse -Force }
        }
    }
}

function Prune-OldBackups {
    $backups = @(Get-ChildItem $UpdateRoot -Directory -Filter "backup-*" -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending)
    if ($backups.Count -gt 3) {
        $backups | Select-Object -Skip 3 | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    }
}

$state = Read-State

if ($Rollback) {
    if (-not $state.last_backup -or -not (Test-Path $state.last_backup)) {
        throw "No rollback backup is available."
    }
    Write-Host "Rolling HCS-AI back to the previous program files..."
    Restore-Backup $state.last_backup
    if (Test-Path ".venv\Scripts\python.exe" -and Test-Path "requirements.txt") {
        & ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    }
    $state.installed_sha = $state.previous_sha
    $state.last_status = "manual_rollback"
    $state.last_update = (Get-Date).ToString("o")
    Write-State $state
    Write-Host "Rollback complete."
    exit 0
}

if ($env:HCS_SKIP_UPDATE -match "^(1|true|yes)$") {
    Write-Host "HCS-AI update check skipped."
    exit 0
}

New-Item -ItemType Directory -Force -Path $UpdateRoot | Out-Null

try {
    $commitUrl = "https://api.github.com/repos/$RepoOwner/$RepoName/commits/$Branch"
    $latest = Invoke-RestMethod -Headers $Headers -Uri $commitUrl -TimeoutSec 15
    $latestSha = [string]$latest.sha
    if (-not $latestSha) { throw "GitHub did not return a commit SHA." }
} catch {
    Write-Warning "Could not check GitHub for HCS-AI updates. Starting the installed version."
    Write-Warning $_.Exception.Message
    exit 0
}

$state.last_check = (Get-Date).ToString("o")
Write-State $state

if (-not $Force -and $state.installed_sha -eq $latestSha) {
    Write-Host "HCS-AI is up to date."
    exit 0
}

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$tempDir = Join-Path ([IO.Path]::GetTempPath()) ("hcs-update-" + [guid]::NewGuid())
$backupDir = Join-Path $UpdateRoot ("backup-" + $stamp)

try {
    Write-Host "HCS-AI update available. Reading update manifest for commit $($latestSha.Substring(0, 8))..."
    $manifestUrl = Get-RawUrl $latestSha "update_manifest.json"
    $updateManifest = Invoke-RestMethod -Headers $Headers -Uri $manifestUrl -TimeoutSec 30
    $files = @($updateManifest.files)
    $deletions = @($updateManifest.delete)
    if ($files.Count -eq 0) { throw "The HCS-AI update manifest did not contain any program files." }

    New-Item -ItemType Directory -Force -Path $tempDir | Out-Null

    # Download every new file before changing the installed application. If a
    # security product or network error blocks a file, the current HCS remains untouched.
    $safeFiles = @()
    foreach ($entry in $files) {
        $path = if ($entry -is [string]) { [string]$entry } else { [string]$entry.path }
        $path = Assert-SafeRelativePath $path
        $safeFiles += $path
        $tempTarget = Join-Path $tempDir ($path -replace "/", "\")
        Ensure-Parent $tempTarget
        Write-Host "Downloading $path"
        Invoke-WebRequest -Headers $Headers -Uri (Get-RawUrl $latestSha $path) -OutFile $tempTarget -TimeoutSec 60
    }

    $safeDeletions = @()
    foreach ($entry in $deletions) {
        $path = if ($entry -is [string]) { [string]$entry } else { [string]$entry.path }
        $safeDeletions += (Assert-SafeRelativePath $path)
    }

    New-Item -ItemType Directory -Force -Path $backupDir | Out-Null
    $manifestEntries = @()
    $allTouched = @($safeFiles + $safeDeletions | Select-Object -Unique)

    foreach ($path in $allTouched) {
        $target = Join-Path $Root ($path -replace "/", "\")
        $existed = Test-Path $target
        $manifestEntries += [pscustomobject]@{ path = $path; existed = $existed }
        if ($existed) {
            $saved = Join-Path $backupDir ($path -replace "/", "\")
            Ensure-Parent $saved
            Copy-Item $target $saved -Recurse -Force
        }
    }

    @{ entries = $manifestEntries } | ConvertTo-Json -Depth 6 |
        Set-Content -Encoding UTF8 (Join-Path $backupDir "manifest.json")

    foreach ($path in $safeFiles) {
        $target = Join-Path $Root ($path -replace "/", "\")
        $source = Join-Path $tempDir ($path -replace "/", "\")
        Ensure-Parent $target
        Copy-Item $source $target -Force
    }

    foreach ($path in $safeDeletions) {
        $target = Join-Path $Root ($path -replace "/", "\")
        if (Test-Path $target) { Remove-Item $target -Recurse -Force }
    }

    if (-not (Test-Path ".venv\Scripts\python.exe")) {
        throw "The HCS-AI virtual environment is missing. Run install.bat once to repair it."
    }

    Write-Host "Updating Python dependencies..."
    & ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw "Dependency update failed with exit code $LASTEXITCODE." }

    Write-Host "Running HCS-AI update self-test..."
    & ".venv\Scripts\python.exe" -m compileall -q hcs_ai
    if ($LASTEXITCODE -ne 0) { throw "Python compile check failed." }

    & ".venv\Scripts\python.exe" -c "from hcs_ai.config import load_config; from hcs_ai.db import init_db; c=load_config(); init_db(); assert c.get('app',{}).get('name') == 'HCS-AI'"
    if ($LASTEXITCODE -ne 0) { throw "HCS-AI startup self-test failed." }

    $state.previous_sha = $state.installed_sha
    $state.installed_sha = $latestSha
    $state.last_backup = $backupDir
    $state.last_status = "success"
    $state.last_update = (Get-Date).ToString("o")
    Write-State $state
    Prune-OldBackups

    Write-Host "HCS-AI updated successfully."
} catch {
    Write-Warning "HCS-AI update failed: $($_.Exception.Message)"
    if (Test-Path (Join-Path $backupDir "manifest.json")) {
        Write-Warning "Restoring the previous HCS-AI program files..."
        try {
            Restore-Backup $backupDir
            if (Test-Path ".venv\Scripts\python.exe" -and Test-Path "requirements.txt") {
                & ".venv\Scripts\python.exe" -m pip install -r requirements.txt | Out-Null
            }
            $state.last_status = "rolled_back_after_failure"
            $state.last_backup = $backupDir
            $state.last_update = (Get-Date).ToString("o")
            Write-State $state
            Write-Warning "Rollback completed. Starting the previous version."
        } catch {
            Write-Error "Automatic rollback also failed: $($_.Exception.Message)"
            exit 1
        }
    }
} finally {
    Remove-Item $tempDir -Recurse -Force -ErrorAction SilentlyContinue
}

exit 0
