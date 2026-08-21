from pathlib import Path
import json

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"

def load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def save_config(config: dict) -> None:
    temp = CONFIG_PATH.with_suffix(".json.tmp")
    temp.write_text(json.dumps(config, indent=2), encoding="utf-8")
    temp.replace(CONFIG_PATH)


def llm_config() -> dict:
    """Return LLM settings with the managed runtime's selected port applied."""
    config = load_config()
    llm = dict(config["llm"])
    if config.get("inference", {}).get("backend") == "llama_cpp":
        state_path = ROOT / "data" / "inference_state.json"
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            if state.get("port"):
                llm["base_url"] = f"http://127.0.0.1:{int(state['port'])}/v1"
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
    return llm
