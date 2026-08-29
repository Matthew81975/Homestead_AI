param([switch]$NoPause)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$runtimeDir = Join-Path $root "runtime\llama.cpp"
$provenancePath = Join-Path $runtimeDir "install-provenance.json"
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
        $releaseCandidates = Invoke-RestMethod -Headers $headers -Uri "https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=30" -TimeoutSec 30
        foreach ($candidate in @($releaseCandidates)) {
            if ($candidate.draft) { continue }
            $candidateAsset = @($candidate.assets) | Where-Object { $_.name -match "^llama-.*-bin-win-cpu-x64\.zip$" } | Select-Object -First 1
            if ($candidateAsset) {
                $asset = $candidateAsset
                $releaseTag = [string]$candidate.tag_name
                break
            }
        }
    } catch {
        throw "Could not query llama.cpp releases from GitHub: $($_.Exception.Message)"
    }

    if (-not $asset) { throw "No recent llama.cpp release contained a Windows CPU x64 package." }

    $assetName = [string]$asset.name
    $assetUrl = [string]$asset.browser_download_url
    if (-not $assetUrl) { throw "GitHub returned the llama.cpp asset without a download URL." }

    Write-Host "Selected llama.cpp release: $releaseTag"
    Write-Host "Selected asset: $assetName"

    $tempBase = Join-Path ([IO.Path]::GetTempPath()) ("hcs-llama-" + [guid]::NewGuid())
    $zipPath = "$tempBase.zip"
    $extractDir = "$tempBase-dir"

    try {
        Write-Host "Downloading $assetName ..."
        & curl.exe -L --fail --retry 3 --retry-delay 2 --output $zipPath $assetUrl
        if ($LASTEXITCODE -ne 0) { throw "The llama.cpp runtime download failed with curl exit code $LASTEXITCODE." }

        if (-not (Test-Path $zipPath)) { throw "The llama.cpp download reported success but no archive was created." }
        $archiveInfo = Get-Item $zipPath
        if ($archiveInfo.Length -lt 1MB) { throw "The downloaded llama.cpp archive is unexpectedly small and was rejected." }

        $archiveSha256 = (Get-FileHash -Algorithm SHA256 -Path $zipPath).Hash.ToLowerInvariant()
        Write-Host "Archive SHA-256: $archiveSha256"

        New-Item -ItemType Directory -Force -Path $extractDir | Out-Null
        Expand-Archive -Path $zipPath -DestinationPath $extractDir -Force

        $server = Get-ChildItem $extractDir -Filter "llama-server.exe" -Recurse | Select-Object -First 1
        if (-not $server) { throw "llama-server.exe was not found in the downloaded llama.cpp package." }

        Copy-Item (Join-Path $server.Directory.FullName "*") $runtimeDir -Recurse -Force
        if (-not (Test-Path $serverExe)) { throw "llama-server.exe was not present after installation." }

        $serverInfo = Get-Item $serverExe
        $serverSha256 = (Get-FileHash -Algorithm SHA256 -Path $serverExe).Hash.ToLowerInvariant()
        Write-Host "llama-server.exe SHA-256: $serverSha256"

        [ordered]@{
            installed_at_utc = [DateTime]::UtcNow.ToString("o")
            source_repository = "ggml-org/llama.cpp"
            release_tag = $releaseTag
            asset_name = $assetName
            asset_url = $assetUrl
            archive_sha256 = $archiveSha256
            archive_bytes = $archiveInfo.Length
            llama_server_sha256 = $serverSha256
            llama_server_bytes = $serverInfo.Length
            llama_server_path = $serverExe
        } | ConvertTo-Json -Depth 4 | Set-Content -Encoding UTF8 -Path $provenancePath
    } finally {
        Remove-Item $zipPath -Force -ErrorAction SilentlyContinue
        Remove-Item $extractDir -Recurse -Force -ErrorAction SilentlyContinue
    }
} else {
    Write-Host "llama.cpp runtime is already installed."
    $serverSha256 = (Get-FileHash -Algorithm SHA256 -Path $serverExe).Hash.ToLowerInvariant()
    Write-Host "Existing llama-server.exe SHA-256: $serverSha256"
}

if (-not (Test-Path $serverExe)) { throw "Setup finished without installing llama-server.exe." }

Write-Host ""
Write-Host "Internal AI runtime installed successfully."
Write-Host "Engine: $serverExe"
Write-Host "Provenance: $provenancePath"
Write-Host ""
Write-Host "SECURITY NOTE:"
Write-Host "Some antivirus products may heuristically flag llama-server.exe because it loads model files and opens a localhost HTTP server."
Write-Host "Do not disable antivirus. Verify the SHA-256/provenance record, then restore only this file and add only the narrow runtime-folder exception if needed."
Write-Host ""
Write-Host "Return to HCS-AI, click Refresh in System, then Start (or Save && Start)."
Write-Host "Your existing GGUF model selection will be preserved."

if (-not $NoPause) { Read-Host "Press Enter to close" }