param([switch]$NoPause)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$runtimeDir = Join-Path $PSScriptRoot "runtime\llama.cpp"
$modelsDir = Join-Path $PSScriptRoot "models"
$modelFile = Join-Path $modelsDir "Qwen3-4B-Instruct-2507-Q4_K_M.gguf"
$repo = "lmstudio-community/Qwen3-4B-Instruct-2507-GGUF"

New-Item -ItemType Directory -Force -Path $runtimeDir, $modelsDir | Out-Null

Write-Host "HCS Internal AI Setup"
Write-Host "====================="

if (-not (Test-Path (Join-Path $runtimeDir "llama-server.exe"))) {
    Write-Host "Downloading the current llama.cpp Windows CPU runtime..."
    $release = Invoke-RestMethod -Headers @{"User-Agent"="HCS-AI-Installer"} `
        -Uri "https://api.github.com/repos/ggml-org/llama.cpp/releases/latest"
    $asset = $release.assets | Where-Object { $_.name -match "bin-win-cpu-x64\.zip$" } | Select-Object -First 1
    if (-not $asset) { throw "The llama.cpp release did not contain a Windows CPU x64 package." }
    $tempDir = Join-Path ([IO.Path]::GetTempPath()) ("hcs-llama-" + [guid]::NewGuid())
    $zipPath = "$tempDir.zip"
    & curl.exe -L --fail --retry 3 --output $zipPath $asset.browser_download_url
    if ($LASTEXITCODE -ne 0) { throw "The llama.cpp runtime download failed with curl exit code $LASTEXITCODE." }
    Expand-Archive -Path $zipPath -DestinationPath $tempDir -Force
    $server = Get-ChildItem $tempDir -Filter "llama-server.exe" -Recurse | Select-Object -First 1
    if (-not $server) { throw "llama-server.exe was not found in the downloaded package." }
    Copy-Item (Join-Path $server.Directory.FullName "*") $runtimeDir -Recurse -Force
    Remove-Item $zipPath -Force
    Remove-Item $tempDir -Recurse -Force
}

if (-not (Test-Path $modelFile)) {
    Write-Host "Finding Qwen3-4B-Instruct-2507 Q4_K_M (approximately 2.5 GB)..."
    $modelInfo = Invoke-RestMethod -Headers @{"User-Agent"="HCS-AI-Installer"} `
        -Uri ("https://huggingface.co/api/models/" + $repo)
    $modelEntry = $modelInfo.siblings | Where-Object { $_.rfilename -match "Q4_K_M\.gguf$" } | Select-Object -First 1
    if (-not $modelEntry) { throw "The recommended Q4_K_M GGUF file was not found." }
    $encodedName = ($modelEntry.rfilename -split "/" | ForEach-Object { [uri]::EscapeDataString($_) }) -join "/"
    $modelUrl = "https://huggingface.co/$repo/resolve/main/$encodedName?download=true"
    Write-Host "Downloading the model. This is the large part; progress may pause briefly..."
    & curl.exe -L --fail --retry 3 --output ($modelFile + ".part") $modelUrl
    if ($LASTEXITCODE -ne 0) { throw "The Qwen model download failed with curl exit code $LASTEXITCODE." }
    if ((Get-Item ($modelFile + ".part")).Length -lt 1GB) {
        throw "The downloaded model file is unexpectedly small and was not installed."
    }
    Move-Item ($modelFile + ".part") $modelFile -Force
}

Write-Host ""
Write-Host "Internal AI is installed. HCS-AI will manage it automatically."
Write-Host "Model: $modelFile"
if (-not $NoPause) { Read-Host "Press Enter to close" }
