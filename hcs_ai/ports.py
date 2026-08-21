import json
import socket

from .config import ROOT, load_config


STATE_PATH = ROOT / "data" / "server_port.json"


def port_candidates():
    server = load_config()["server"]
    first = int(server.get("port", 8765))
    attempts = max(1, int(server.get("port_attempts", 100)))
    return range(first, min(first + attempts, 65536))


def port_is_available(host: str, port: int) -> bool:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            sock.bind((host, port))
        return True
    except OSError:
        return False


def choose_port(host: str) -> int:
    for port in port_candidates():
        if port_is_available(host, port):
            return port
    raise RuntimeError("No available HCS-AI server port was found in the configured range.")


def save_selected_port(host: str, port: int) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps({"host": host, "port": port}, indent=2), encoding="utf-8"
    )


def saved_endpoint():
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return str(state["host"]), int(state["port"])
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None
