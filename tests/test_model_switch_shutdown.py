from pathlib import Path

from hcs_ai import config
from hcs_ai import desktop_home


def test_managed_llama_cpp_forces_auto_model(monkeypatch, tmp_path: Path):
    state = tmp_path / "inference_state.json"
    state.write_text('{"port": 4321}', encoding="utf-8")
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(config, "load_config", lambda: {
        "llm": {"base_url": "http://127.0.0.1:9999/v1", "model": "stale-model"},
        "inference": {"backend": "llama_cpp"},
    })

    llm = config.llm_config()

    assert llm["base_url"] == "http://127.0.0.1:4321/v1"
    assert llm["model"] == "auto"


def test_desktop_home_maps_window_close_to_full_exit():
    assert desktop_home.desktop_host.DesktopHost.hide_window is desktop_home.desktop_host.DesktopHost.exit
