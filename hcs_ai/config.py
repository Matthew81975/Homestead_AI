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

    default_app = default_config.get("app", {})
    if default_app:
        app = config.setdefault("app", {})
        for key in ("name", "version", "system_prompt"):
            if key in default_app:
                app[key] = default_app[key]

    return config


def save_config(config: dict) -> None:
    temp = LOCAL_CONFIG_PATH.with_suffix(".json.tmp")
    temp.write_text(json.dumps(config, indent=2), encoding="utf-8")
    temp.replace(LOCAL_CONFIG_PATH)


def llm_config() -> dict:
    """Return settings for HCS-managed local inference.

    Offline mode is intentionally strict: HCS will only use its own managed
    llama.cpp server and will not fall through to an unrelated OpenAI-compatible
    server that happens to be listening on the legacy port.
    """
    config = load_config()
    mode = str(config.get("ai", {}).get("mode") or "offline").lower()
    if mode == "live":
        raise RuntimeError(
            "Live/cloud AI is selected but no cloud provider is configured yet. "
            "Switch to Offline or configure a cloud provider."
        )

    llm = dict(config["llm"])
    llm["model"] = "auto"
    if config.get("inference", {}).get("backend") == "llama_cpp":
        state_path = ROOT / "data" / "inference_state.json"
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            state = {}
        if state.get("phase") != "ready" or not state.get("port"):
            raise RuntimeError(
                "The HCS internal llama.cpp engine is not ready. "
                "Use Internal AI Setup in the System tab, then start the engine."
            )
        llm["base_url"] = f"http://127.0.0.1:{int(state['port'])}/v1"
    return llm
