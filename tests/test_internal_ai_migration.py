import tempfile
import unittest
from pathlib import Path
from hcs_ai import repair_internal_ai_setup

OLD = '''    $release = Invoke-RestMethod -Headers @{"User-Agent"="HCS-AI-Installer"} `
        -Uri "https://api.github.com/repos/ggml-org/llama.cpp/releases/latest"
    $asset = $release.assets | Where-Object { $_.name -match "bin-win-cpu-x64\\.zip$" } | Select-Object -First 1
    if (-not $asset) { throw "The llama.cpp release did not contain a Windows CPU x64 package." }
'''


class InternalAISetupMigrationTests(unittest.TestCase):
    def test_replaces_latest_release_lookup_with_recent_release_scan(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            script = root / "setup_internal_ai.ps1"
            script.write_text("before\n" + OLD + "after\n", encoding="utf-8")
            self.assertTrue(repair_internal_ai_setup(root))
            text = script.read_text(encoding="utf-8")
            self.assertIn("releases?per_page=20", text)
            self.assertIn("foreach ($candidate in $releaseCandidates)", text)
            self.assertNotIn("releases/latest", text)


if __name__ == "__main__":
    unittest.main()
