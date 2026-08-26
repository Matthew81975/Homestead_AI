import json
from pathlib import Path

from hcs_ai import engine


def test_status_reports_startup_phase_and_error(monkeypatch, tmp_path: Path):
    state_path = tmp_path / "inference_state.json"
    state_path.write_text(json.dumps({"phase": "failed", "error": "model failed"}), encoding="utf-8")
    monkeypatch.setattr(engine, "STATE_PATH", state_path)
    monkeypatch.setattr(engine, "load_config", lambda: {
        "inference": {
            "backend": "llama_cpp",
            "executable": "missing.exe",
            "model_path": "missing.gguf",
            "auto_start": True,
        }
    })
    monkeypatch.setattr(engine, "_process", None)

    status = engine.status()

    assert status["phase"] == "failed"
    assert status["error"] == "model failed"
