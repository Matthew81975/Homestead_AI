import json
import socket
import time
import urllib.request

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


def _hcs_health_ready(host: str, port: int, timeout: float = 0.5) -> bool:
    """Return True only when the saved HCS server is actually answering HTTP health checks."""
    try:
        url = f"http://{host}:{port}/health"
        with urllib.request.urlopen(url, timeout=timeout) as response:
            health = json.loads(response.read().decode("utf-8"))
        return health.get("name") == "HCS-AI"
    except Exception:
        return False


def saved_endpoint(wait_seconds: float = 30.0):
    """Return the endpoint selected by the server, waiting for that exact server to become ready.

    The launcher writes server_port.json before Uvicorn finishes application startup. Without a
    short readiness wait, the GUI can race ahead, fail the new port once, then attach to an older
    HCS process still listening on the default port. Waiting here keeps the GUI pinned to the
    server instance that the current launch selected.
    """
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        endpoint = str(state["host"]), int(state["port"])
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None

    deadline = time.monotonic() + max(0.0, float(wait_seconds))
    while time.monotonic() < deadline:
        if _hcs_health_ready(*endpoint):
            return endpoint
        time.sleep(0.2)

    # Return the selected endpoint even if startup is unusually slow. The GUI's normal
    # discovery/retry loop will continue handling temporary failures after this point.
    return endpoint
