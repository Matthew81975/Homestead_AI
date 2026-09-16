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


def test_status_survives_inaccessible_configured_paths(monkeypatch, tmp_path: Path):
    class InaccessiblePath:
        def __init__(self, value):
            self.value = value

        def __str__(self):
            return self.value

        def is_file(self):
            raise OSError(1005, "The volume does not contain a recognized file system")

    monkeypatch.setattr(engine, "STATE_PATH", tmp_path / "missing-state.json")
    monkeypatch.setattr(engine, "load_config", lambda: {
        "inference": {
            "backend": "llama_cpp",
            "executable": "X:/llama-server.exe",
            "model_path": "X:/model.gguf",
        }
    })
    monkeypatch.setattr(engine, "_resolve", lambda value: InaccessiblePath(value))
    monkeypatch.setattr(engine, "_process", None)

    status = engine.status()

    assert status["executable_found"] is False
    assert status["model_found"] is False
    assert "recognized file system" in status["executable_error"]
    assert "recognized file system" in status["model_error"]
