import argparse
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request

import psutil
import pystray
from PIL import Image, ImageDraw
from tkinter import messagebox

from .config import ROOT, load_config
from .gui_tree import App
from .ports import STATE_PATH

REPO_OWNER = "Matthew81975"
REPO_NAME = "Homestead_AI"
UPDATE_STATE = ROOT / ".hcs-update" / "state.json"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "HCS-AI"


def _version_tuple(value):
    out = []
    for part in str(value).split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        out.append(int(digits or 0))
    return tuple(out)


def _health(host, port, timeout=0.5):
    base = f"http://{host}:{port}"
    with urllib.request.urlopen(base + "/health", timeout=timeout) as response:
        return base, json.loads(response.read().decode("utf-8"))


def _startup_command():
    batch = ROOT / "run_hcs_ai.bat"
    return f'cmd.exe /c "\"{batch}\" --minimized"'


def startup_enabled():
    if os.name != "nt":
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, RUN_VALUE)
        return str(value).strip() == _startup_command()
    except OSError:
        return False


def set_startup(enabled=True):
    if os.name != "nt":
        return
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        if enabled:
            winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, _startup_command())
        else:
            try:
                winreg.DeleteValue(key, RUN_VALUE)
            except FileNotFoundError:
                pass


def _tray_image():
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((6, 6, 58, 58), radius=12, fill=(42, 42, 48, 255))
    draw.text((20, 15), "H", fill=(255, 255, 255, 255))
    return image


def latest_commit_sha():
    url = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/commits/main"
    request = urllib.request.Request(url, headers={"User-Agent": "HCS-AI-Update-Check"})
    with urllib.request.urlopen(request, timeout=12) as response:
        data = json.loads(response.read().decode("utf-8"))
    return str(data.get("sha") or "")


def installed_commit_sha():
    try:
        data = json.loads(UPDATE_STATE.read_text(encoding="utf-8"))
        return str(data.get("installed_sha") or "")
    except (OSError, ValueError, TypeError):
        return ""


def terminate_process_tree(pid):
    """Terminate a process and all descendants so Exit HCS releases model/server RAM."""
    try:
        parent = psutil.Process(pid)
    except psutil.Error:
        return
    children = parent.children(recursive=True)
    for proc in children:
        try:
            proc.terminate()
        except psutil.Error:
            pass
    gone, alive = psutil.wait_procs(children, timeout=4)
    for proc in alive:
        try:
            proc.kill()
        except psutil.Error:
            pass
    try:
        parent.terminate()
        parent.wait(timeout=4)
    except psutil.TimeoutExpired:
        try:
            parent.kill()
        except psutil.Error:
            pass
    except psutil.Error:
        pass


class DesktopHost:
    def __init__(self, minimized=False):
        self.minimized = minimized
        self.server = None
        self.icon = None
        self.app = None
        self.exiting = False
        self.expected_version = str(load_config().get("app", {}).get("version", "0.0.0"))

    def start_server(self):
        try:
            STATE_PATH.unlink(missing_ok=True)
        except OSError:
            pass
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.server = subprocess.Popen(
            [sys.executable, "-m", "hcs_ai.server_tree"],
            cwd=str(ROOT),
            creationflags=flags,
        )

    def wait_for_server(self, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.server and self.server.poll() is not None:
                raise RuntimeError("The HCS-AI server exited during startup.")
            try:
                state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
                host, port = str(state["host"]), int(state["port"])
                base, health = _health(host, port)
                if health.get("name") != "HCS-AI":
                    raise RuntimeError("Selected endpoint is not HCS-AI.")
                if _version_tuple(health.get("version", "0")) < _version_tuple(self.expected_version):
                    raise RuntimeError("Selected endpoint is an older HCS-AI server.")
                return base, health
            except Exception:
                time.sleep(0.35)
        raise TimeoutError("HCS-AI server did not become ready within 30 seconds.")

    def open_window(self, *_):
        if self.app:
            self.app.after(0, self._show_window)

    def _show_window(self):
        self.app.deiconify()
        self.app.lift()
        try:
            self.app.focus_force()
        except Exception:
            pass

    def hide_window(self):
        if self.app:
            self.app.withdraw()

    def check_for_updates(self, *_):
        threading.Thread(target=self._check_for_updates_worker, daemon=True).start()

    def _check_for_updates_worker(self):
        try:
            latest = latest_commit_sha()
            installed = installed_commit_sha()
            if latest and latest != installed:
                self.app.after(0, self._prompt_restart_for_update)
            else:
                self.app.after(0, lambda: messagebox.showinfo("HCS-AI Updates", "HCS-AI is up to date."))
        except Exception as exc:
            self.app.after(0, lambda msg=str(exc): messagebox.showwarning(
                "HCS-AI Updates", f"Could not check for updates.\n\n{msg}"
            ))

    def _prompt_restart_for_update(self):
        if messagebox.askyesno(
            "HCS-AI Update Available",
            "A new HCS-AI update is available. Restart HCS now to install it?",
        ):
            self.restart()

    def toggle_startup(self, *_):
        try:
            set_startup(not startup_enabled())
            if self.icon:
                self.icon.update_menu()
        except Exception as exc:
            self.app.after(0, lambda: messagebox.showerror("HCS-AI Startup", str(exc)))

    def restart(self, *_):
        if self.exiting:
            return
        self.exiting = True
        self._stop_children()
        batch = ROOT / "run_hcs_ai.bat"
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        subprocess.Popen(["cmd.exe", "/c", "start", "", str(batch)], cwd=str(ROOT), creationflags=flags)
        self._finish_exit()

    def exit(self, *_):
        """Fully stop HCS, including the local model, to release its memory."""
        if self.exiting:
            return
        self.exiting = True
        self._stop_children()
        self._finish_exit()

    def _stop_children(self):
        if self.server and self.server.poll() is None:
            terminate_process_tree(self.server.pid)

    def _finish_exit(self):
        if self.icon:
            try:
                self.icon.stop()
            except Exception:
                pass
        if self.app:
            try:
                self.app.after(0, self.app.destroy)
            except Exception:
                pass

    def build_tray(self):
        menu = pystray.Menu(
            pystray.MenuItem("Open HCS", self.open_window, default=True),
            pystray.MenuItem("Check for Updates", self.check_for_updates),
            pystray.MenuItem("Restart HCS", self.restart),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Start with Windows", self.toggle_startup, checked=lambda _item: startup_enabled()),
            pystray.MenuItem("Exit HCS (free memory)", self.exit),
        )
        self.icon = pystray.Icon("HCS-AI", _tray_image(), "HCS-AI", menu)
        threading.Thread(target=self.icon.run, daemon=True).start()

    def run(self):
        try:
            if os.name == "nt" and not startup_enabled():
                set_startup(True)
        except Exception:
            pass

        self.start_server()
        _base, health = self.wait_for_server()
        self.app = App()
        self.app.title(f"HCS-AI {health.get('version', self.expected_version)}")
        self.app.protocol("WM_DELETE_WINDOW", self.hide_window)
        self.build_tray()

        if self.minimized:
            self.app.withdraw()
        else:
            self._show_window()
        self.app.mainloop()

        if not self.exiting:
            self.exiting = True
            self._stop_children()
            if self.icon:
                try:
                    self.icon.stop()
                except Exception:
                    pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--minimized", action="store_true")
    args = parser.parse_args()
    DesktopHost(minimized=args.minimized).run()


if __name__ == "__main__":
    main()
