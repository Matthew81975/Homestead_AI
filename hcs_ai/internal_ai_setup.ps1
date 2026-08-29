param([switch]$NoPause)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$runtimeDir = Join-Path $root "runtime\llama.cpp"
New-Item -ItemType Directory -Force -Path $runtimeDir | Out-Null

Write-Host "HCS Internal AI Setup"
Write-Host "====================="
Write-Host ""

$serverExe = Join-Path $runtimeDir "llama-server.exe"

if (-not (Test-Path $serverExe)) {
    Write-Host "Installing the current llama.cpp Windows CPU runtime..."
    Write-Host "Resolving a release that actually contains the Windows CPU x64 package..."

    $headers = @{
        "User-Agent" = "HCS-AI-Installer"
        "Accept" = "application/vnd.github+json"
    }

    $asset = $null
    $releaseTag = $null

    try {
        # Do not trust /releases/latest here. Inspect real release assets and
        # choose one that actually contains the Windows CPU archive HCS needs.
        $releaseCandidates = Invoke-RestMethod -Headers $headers -Uri "https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=30" -TimeoutSec 30

        foreach ($candidate in @($releaseCandidates)) {
            if ($candidate.draft) { continue }

            $candidateAsset = @($candidate.assets) |
                Where-Object { $_.name -match "^llama-.*-bin-win-cpu-x64\.zip$" } |
                Select-Object -First 1

            if ($candidateAsset) {
                $asset = $candidateAsset
                $releaseTag = [string]$candidate.tag_name
                break
            }
        }
    }
    catch {
        throw "Could not query llama.cpp releases from GitHub: $($_.Exception.Message)"
    }

    if (-not $asset) {
        throw "No recent llama.cpp release contained a Windows CPU x64 package."
    }

    $assetName = [string]$asset.name
    $assetUrl = [string]$asset.browser_download_url

    if (-not $assetUrl) {
        throw "GitHub returned the llama.cpp asset without a download URL."
    }

    Write-Host "Selected llama.cpp release: $releaseTag"
    Write-Host "Selected asset: $assetName"

    $tempBase = Join-Path ([IO.Path]::GetTempPath()) ("hcs-llama-" + [guid]::NewGuid())
    $zipPath = "$tempBase.zip"
    $extractDir = "$tempBase-dir"

    try {
        Write-Host "Downloading $assetName ..."
        & curl.exe -L --fail --retry 3 --retry-delay 2 --output $zipPath $assetUrl
        if ($LASTEXITCODE -ne 0) {
            throw "The llama.cpp runtime download failed with curl exit code $LASTEXITCODE."
        }

        if (-not (Test-Path $zipPath)) {
            throw "The llama.cpp download reported success but no archive was created."
        }

        if ((Get-Item $zipPath).Length -lt 1MB) {
            throw "The downloaded llama.cpp archive is unexpectedly small and was rejected."
        }

        New-Item -ItemType Directory -Force -Path $extractDir | Out-Null
        Expand-Archive -Path $zipPath -DestinationPath $extractDir -Force

        $server = Get-ChildItem $extractDir -Filter "llama-server.exe" -Recurse | Select-Object -First 1
        if (-not $server) {
            throw "llama-server.exe was not found in the downloaded llama.cpp package."
        }

        Copy-Item (Join-Path $server.Directory.FullName "*") $runtimeDir -Recurse -Force
    }
    finally {
        Remove-Item $zipPath -Force -ErrorAction SilentlyContinue
        Remove-Item $extractDir -Recurse -Force -ErrorAction SilentlyContinue
    }
} else {
    Write-Host "llama.cpp runtime is already installed."
}

if (-not (Test-Path $serverExe)) {
    throw "Setup finished without installing llama-server.exe."
}

Write-Host ""
Write-Host "Internal AI runtime installed successfully."
Write-Host "Engine: $serverExe"
Write-Host ""
Write-Host "Return to HCS-AI, click Refresh in System, then Start (or Save && Start)."
Write-Host "Your existing GGUF model selection will be preserved."

if (-not $NoPause) {
    Read-Host "Press Enter to close"
}
