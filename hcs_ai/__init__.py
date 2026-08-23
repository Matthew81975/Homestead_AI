from pathlib import Path

__version__ = "0.5.0"

_OLD_LLAMA_RELEASE_LOOKUP = '''    $release = Invoke-RestMethod -Headers @{"User-Agent"="HCS-AI-Installer"} `
        -Uri "https://api.github.com/repos/ggml-org/llama.cpp/releases/latest"
    $asset = $release.assets | Where-Object { $_.name -match "bin-win-cpu-x64\\.zip$" } | Select-Object -First 1
    if (-not $asset) { throw "The llama.cpp release did not contain a Windows CPU x64 package." }
'''

_NEW_LLAMA_RELEASE_LOOKUP = '''    $releaseCandidates = Invoke-RestMethod -Headers @{"User-Agent"="HCS-AI-Installer"} `
        -Uri "https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=20"
    $asset = $null
    foreach ($candidate in $releaseCandidates) {
        $asset = $candidate.assets | Where-Object { $_.name -match "bin-win-cpu-x64\\.zip$" } | Select-Object -First 1
        if ($asset) { break }
    }
    if (-not $asset) { throw "No recent llama.cpp release contained a Windows CPU x64 package." }
'''


def repair_internal_ai_setup(root=None):
    """Migrate installs that assumed GitHub's /releases/latest always had binaries."""
    root = Path(root) if root is not None else Path(__file__).resolve().parent.parent
    script = root / "setup_internal_ai.ps1"
    try:
        text = script.read_text(encoding="utf-8")
    except OSError:
        return False
    repaired = text.replace(_OLD_LLAMA_RELEASE_LOOKUP, _NEW_LLAMA_RELEASE_LOOKUP)
    if repaired == text:
        return False
    try:
        script.write_text(repaired, encoding="utf-8")
    except OSError:
        return False
    return True


repair_internal_ai_setup()
