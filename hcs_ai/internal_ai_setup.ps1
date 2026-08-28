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
    Write-Host "Resolving the latest release without using the GitHub API..."

    $latestUrl = "https://github.com/ggml-org/llama.cpp/releases/latest"
    $effective = (& curl.exe -L -s -o NUL -w "%{url_effective}" $latestUrl).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $effective) {
        throw "Could not resolve the latest llama.cpp release from GitHub."
    }

    if ($effective -notmatch "/releases/tag/([^/?#]+)") {
        throw "Could not determine the llama.cpp release tag from: $effective"
    }
    $tag = $Matches[1]
    Write-Host "Latest llama.cpp release: $tag"

    $assetName = "llama-$tag-bin-win-cpu-x64.zip"
    $assetUrl = "https://github.com/ggml-org/llama.cpp/releases/download/$tag/$assetName"

    $tempBase = Join-Path ([IO.Path]::GetTempPath()) ("hcs-llama-" + [guid]::NewGuid())
    $zipPath = "$tempBase.zip"
    $extractDir = "$tempBase-dir"

    try {
        Write-Host "Downloading $assetName ..."
        & curl.exe -L --fail --retry 3 --retry-delay 2 --output $zipPath $assetUrl
        if ($LASTEXITCODE -ne 0) {
            throw "The llama.cpp runtime download failed with curl exit code $LASTEXITCODE."
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
