$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$installLog = Join-Path $PSScriptRoot "install.log"
Start-Transcript -Path $installLog -Append | Out-Null

Write-Host "HCS-AI v0.8 installer"
Write-Host "---------------------"

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Host "Python was not found on PATH."
    Write-Host "Install Python 3.10+ and enable 'Add Python to PATH', then run this installer again."
    exit 1
}
python -c "import sys; assert sys.version_info >= (3,10), 'Python 3.10+ required'"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
if (-not (Test-Path ".venv")) { python -m venv .venv }
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# config.default.json is version-controlled. config.json belongs to this PC and
# is preserved across automatic updates.
if (-not (Test-Path "config.json") -and (Test-Path "config.default.json")) {
    Copy-Item "config.default.json" "config.json"
}

# Desktop + Start Menu shortcuts are standard for HCS apps.
$ws = New-Object -ComObject WScript.Shell
$target = Join-Path $PSScriptRoot "run_hcs_ai.bat"
$desktop = [Environment]::GetFolderPath("Desktop")
$startMenu = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"
foreach ($linkPath in @((Join-Path $desktop "HCS-AI.lnk"), (Join-Path $startMenu "HCS-AI.lnk"))) {
    $sc = $ws.CreateShortcut($linkPath)
    $sc.TargetPath = $target
    $sc.WorkingDirectory = $PSScriptRoot
    $sc.Description = "Homestead Computer Systems AI"
    $sc.Save()
}
Write-Host ""
Write-Host "Installation complete. Desktop and Start Menu shortcuts created."
Write-Host "Future program updates will be checked automatically when HCS-AI starts."
$answer = Read-Host "Install the internal llama.cpp engine and recommended Qwen model now? [Y/n]"
if ([string]::IsNullOrWhiteSpace($answer) -or $answer -match "^[Yy]") {
    try {
        & (Join-Path $PSScriptRoot "setup_internal_ai.ps1") -NoPause
    } catch {
        $setupError = "$(Get-Date -Format o) $($_.Exception.ToString())"
        Add-Content -Path (Join-Path $PSScriptRoot "internal_ai_setup.log") -Value $setupError
        Write-Warning "Internal AI setup did not finish: $($_.Exception.Message)"
        Write-Warning "HCS-AI itself is installed and will still launch."
        Write-Warning "See internal_ai_setup.log, then retry from System > Internal AI Setup."
        $global:LASTEXITCODE = 0
    }
}
Write-Host "HCS-AI will manage its local model automatically; LM Studio is optional."
Stop-Transcript | Out-Null
