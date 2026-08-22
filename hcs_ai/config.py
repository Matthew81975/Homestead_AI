from pathlib import Path
import json

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = ROOT / "config.default.json"
LOCAL_CONFIG_PATH = ROOT / "config.json"
CONFIG_PATH = LOCAL_CONFIG_PATH


def _deep_merge(base: dict, override: dict) -> dict:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config() -> dict:
    default_config = {}
    local_config = {}

    if DEFAULT_CONFIG_PATH.exists():
        default_config = json.loads(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))

    if LOCAL_CONFIG_PATH.exists():
        local_config = json.loads(LOCAL_CONFIG_PATH.read_text(encoding="utf-8"))

    if not default_config and not local_config:
        raise FileNotFoundError("No HCS-AI configuration file was found.")

    config = _deep_merge(default_config, local_config)

    # The application version is part of the installed code, not a user setting.
    # Keep local configuration from pinning HCS to an old displayed version.
    default_version = default_config.get("app", {}).get("version")
    if default_version:
        config.setdefault("app", {})["version"] = default_version

    return config


def save_config(config: dict) -> None:
    # Save only to the local configuration file. The default configuration is
    # version-controlled and can safely change during automatic updates.
    temp = LOCAL_CONFIG_PATH.with_suffix(".json.tmp")
    temp.write_text(json.dumps(config, indent=2), encoding="utf-8")
    temp.replace(LOCAL_CONFIG_PATH)


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
