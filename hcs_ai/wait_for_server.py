import json
import sys
import time
import urllib.request

from .ports import STATE_PATH


def _version_tuple(value: str):
    parts = []
    for piece in str(value).split("."):
        try:
            parts.append(int(piece))
        except ValueError:
            break
    return tuple(parts)


def main():
    min_version = sys.argv[1] if len(sys.argv) > 1 else "0.8.3"
    deadline = time.time() + 30
    last_error = "server state not ready"

    while time.time() < deadline:
        try:
            state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            host = str(state["host"])
            port = int(state["port"])
            url = f"http://{host}:{port}/health"
            with urllib.request.urlopen(url, timeout=1.0) as response:
                health = json.loads(response.read().decode("utf-8"))
            if health.get("name") != "HCS-AI":
                raise RuntimeError("selected endpoint is not HCS-AI")
            if _version_tuple(health.get("version", "0")) < _version_tuple(min_version):
                raise RuntimeError(
                    f"selected HCS-AI server is {health.get('version')}; need {min_version}+"
                )
            print(f"HCS-AI server ready at {url} ({health.get('version')})")
            return 0
        except Exception as exc:
            last_error = str(exc)
            time.sleep(0.5)

    print(f"HCS-AI server did not become ready: {last_error}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
