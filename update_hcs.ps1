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

$PreserveTopLevel = @(
    ".git", ".github", ".venv", ".hcs-update", "data", "models", "runtime",
    "config.json", ".env", "secrets.json"
)

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

function Restore-Backup([string]$backupDir) {
    $manifestPath = Join-Path $backupDir "manifest.json"
    if (-not (Test-Path $manifestPath)) { throw "Rollback manifest not found: $manifestPath" }
    $manifest = Get-Content $manifestPath -Raw | ConvertFrom-Json

    foreach ($entry in $manifest.entries) {
        $target = Join-Path $Root $entry.name
        if (Test-Path $target) { Remove-Item $target -Recurse -Force }
        if ($entry.existed) {
            $saved = Join-Path $backupDir $entry.name
            if (Test-Path $saved) { Copy-Item $saved $target -Recurse -Force }
        }
    }
}

$state = Read-State

if ($Rollback) {
    if (-not $state.last_backup -or -not (Test-Path $state.last_backup)) {
        throw "No rollback backup is available."
    }
    Write-Host "Rolling HCS-AI back to the previous program files..."
    Restore-Backup $state.last_backup
    if (Test-Path ".venv\Scripts\python.exe") {
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
$tempBase = Join-Path ([IO.Path]::GetTempPath()) ("hcs-update-" + [guid]::NewGuid())
$zipPath = "$tempBase.zip"
$extractDir = "$tempBase-extracted"
$backupDir = Join-Path $UpdateRoot ("backup-" + $stamp)

try {
    Write-Host "HCS-AI update available. Downloading..."
    $archiveUrl = "https://github.com/$RepoOwner/$RepoName/archive/refs/heads/$Branch.zip"
    Invoke-WebRequest -Headers $Headers -Uri $archiveUrl -OutFile $zipPath -TimeoutSec 120
    Expand-Archive -Path $zipPath -DestinationPath $extractDir -Force

    $sourceRoot = Get-ChildItem $extractDir -Directory | Select-Object -First 1
    if (-not $sourceRoot) { throw "Downloaded HCS-AI archive was empty." }

    New-Item -ItemType Directory -Force -Path $backupDir | Out-Null
    $manifestEntries = @()

    foreach ($item in Get-ChildItem $sourceRoot.FullName -Force) {
        if ($PreserveTopLevel -contains $item.Name) { continue }
        if ($item.Name -eq "config.default.json") { }
        $target = Join-Path $Root $item.Name
        $existed = Test-Path $target
        $manifestEntries += [pscustomobject]@{ name = $item.Name; existed = $existed }
        if ($existed) {
            Copy-Item $target (Join-Path $backupDir $item.Name) -Recurse -Force
        }
    }

    @{ entries = $manifestEntries } | ConvertTo-Json -Depth 6 | Set-Content -Encoding UTF8 (Join-Path $backupDir "manifest.json")

    foreach ($item in Get-ChildItem $sourceRoot.FullName -Force) {
        if ($PreserveTopLevel -contains $item.Name) { continue }
        $target = Join-Path $Root $item.Name
        if (Test-Path $target) { Remove-Item $target -Recurse -Force }
        Copy-Item $item.FullName $target -Recurse -Force
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
    Remove-Item $zipPath -Force -ErrorAction SilentlyContinue
    Remove-Item $extractDir -Recurse -Force -ErrorAction SilentlyContinue
}

exit 0
