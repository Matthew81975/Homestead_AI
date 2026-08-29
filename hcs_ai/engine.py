import json
import os
import shlex
import socket
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

from .config import ROOT, load_config, save_config


STATE_PATH = ROOT / "data" / "inference_state.json"
LOG_PATH = ROOT / "data" / "llama-server.log"
_lock = threading.RLock()
_process = None
_log_handle = None
_last_command = []


def _resolve(path_value):
    path = Path(path_value or "").expanduser()
    return path if path.is_absolute() else ROOT / path


def _available_port(host, first, attempts=100):
    for port in range(int(first), min(int(first) + int(attempts), 65536)):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.bind((host, port))
            return port
        except OSError:
            continue
    raise RuntimeError("No free port was found for the internal llama.cpp server.")


def _write_state(**state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _log_tail(max_chars=12000):
    try:
        text = LOG_PATH.read_text(encoding="utf-8", errors="replace")
        return text[-max_chars:].strip()
    except OSError:
        return ""


def _format_command(args):
    try:
        return subprocess.list2cmdline([str(x) for x in args])
    except Exception:
        return " ".join(shlex.quote(str(x)) for x in args)


def status():
    cfg = load_config().get("inference", {})
    exe = _resolve(cfg.get("executable"))
    model = _resolve(cfg.get("model_path"))
    running = _process is not None and _process.poll() is None
    state = {}
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    ready = False
    if running and state.get("port"):
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{state['port']}/v1/models", timeout=0.35
            ) as response:
                ready = response.status == 200
        except Exception:
            pass
    phase = state.get("phase")
    if not phase:
        phase = "ready" if ready else ("starting" if running else "stopped")
    return {
        "backend": cfg.get("backend", "external"),
        "running": running,
        "ready": ready,
        "phase": phase,
        "error": state.get("error"),
        "exit_code": state.get("exit_code"),
        "pid": _process.pid if running else state.get("pid"),
        "port": state.get("port"),
        "executable": str(exe),
        "executable_found": exe.is_file(),
        "model_path": str(model),
        "model_found": model.is_file(),
        "command": state.get("command"),
        "auto_start": bool(cfg.get("auto_start", True)),
        "log_path": str(LOG_PATH),
    }


def _startup_failure_message(exit_code=None):
    cfg = load_config().get("inference", {})
    exe = _resolve(cfg.get("executable"))
    model = _resolve(cfg.get("model_path"))
    command = _format_command(_last_command) if _last_command else "(command unavailable)"
    tail = _log_tail()
    pieces = [
        "The selected model server exited before becoming ready.",
        f"Exit code: {exit_code if exit_code is not None else 'unknown'}",
        f"Engine: {exe}",
        f"Model: {model}",
        f"Command: {command}",
    ]
    if tail:
        pieces.append("llama-server log:\n" + tail)
    return "\n".join(pieces)


def _wait_until_ready(timeout_seconds=90):
    deadline = time.monotonic() + max(1.0, float(timeout_seconds))
    while time.monotonic() < deadline:
        if _process is None:
            raise RuntimeError(_startup_failure_message(None))
        exit_code = _process.poll()
        if exit_code is not None:
            raise RuntimeError(_startup_failure_message(exit_code))
        current = status()
        if current.get("ready"):
            return current
        time.sleep(0.25)
    tail = _log_tail()
    detail = f"\n\nllama-server log:\n{tail}" if tail else ""
    command = _format_command(_last_command) if _last_command else "(command unavailable)"
    raise RuntimeError(
        f"The selected model did not become ready within {timeout_seconds:.0f} seconds.\n"
        f"Command: {command}{detail}"
    )


def start():
    global _process, _log_handle, _last_command
    with _lock:
        if _process is not None and _process.poll() is None:
            current = status()
            if current.get("ready"):
                return current
        config = load_config()
        cfg = config.get("inference", {})
        if cfg.get("backend") != "llama_cpp":
            raise RuntimeError("The configured inference backend is not llama.cpp.")
        exe, model = _resolve(cfg.get("executable")), _resolve(cfg.get("model_path"))
        if not exe.is_file():
            raise RuntimeError("llama-server.exe is not installed. Run Internal AI Setup.")
        if not model.is_file():
            raise RuntimeError("No GGUF model is installed or selected. Run Internal AI Setup or choose a model.")
        host = cfg.get("host", "127.0.0.1")
        port = _available_port(host, cfg.get("port", 1234), cfg.get("port_attempts", 100))
        args = [
            str(exe), "-m", str(model), "--host", host, "--port", str(port),
            "-c", str(int(cfg.get("context_size", 4096))),
            "-t", str(int(cfg.get("threads", max(1, (os.cpu_count() or 4) // 2)))),
            "--jinja",
        ]
        args.extend(str(x) for x in cfg.get("extra_args", []))
        _last_command = list(args)
        command = _format_command(args)

        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        _log_handle = LOG_PATH.open("a", encoding="utf-8")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        _process = subprocess.Popen(
            args, cwd=str(ROOT), stdout=_log_handle, stderr=subprocess.STDOUT,
            creationflags=flags,
        )
        _write_state(
            port=port,
            pid=_process.pid,
            model_path=str(model),
            executable=str(exe),
            command=command,
            phase="starting",
            error=None,
            exit_code=None,
        )
        try:
            _wait_until_ready(float(cfg.get("startup_timeout_seconds", 90)))
            _write_state(
                port=port,
                pid=_process.pid,
                model_path=str(model),
                executable=str(exe),
                command=command,
                phase="ready",
                error=None,
                exit_code=None,
            )
            return status()
        except Exception as exc:
            exit_code = None
            if _process is not None:
                exit_code = _process.poll()
            if _process is not None and _process.poll() is None:
                _process.terminate()
                try:
                    _process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    _process.kill()
                    _process.wait(timeout=2)
                exit_code = _process.returncode
            if _log_handle:
                _log_handle.close()
            _process = None
            _log_handle = None
            _write_state(
                port=port,
                pid=None,
                model_path=str(model),
                executable=str(exe),
                command=command,
                phase="failed",
                error=str(exc),
                exit_code=exit_code,
            )
            raise


def stop():
    global _process, _log_handle
    with _lock:
        if _process is not None and _process.poll() is None:
            _process.terminate()
            try:
                _process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                _process.kill()
        _process = None
        if _log_handle:
            _log_handle.close()
        _log_handle = None
        cfg = load_config().get("inference", {})
        _write_state(
            model_path=str(_resolve(cfg.get("model_path"))),
            phase="stopped",
            error=None,
            exit_code=None,
        )
        return status()


def configure(model_path, auto_start=True):
    config = load_config()
    config.setdefault("inference", {})["model_path"] = model_path
    config["inference"]["auto_start"] = bool(auto_start)
    if config["inference"].get("backend") == "llama_cpp":
        config.setdefault("llm", {})["model"] = "auto"
    save_config(config)
    return status()


def auto_start():
    cfg = load_config().get("inference", {})
    if cfg.get("backend") == "llama_cpp" and cfg.get("auto_start", True):
        try:
            start()
        except Exception as exc:
            _write_state(
                model_path=str(_resolve(cfg.get("model_path"))),
                phase="failed",
                error=str(exc),
                exit_code=_process.poll() if _process is not None else None,
            )
