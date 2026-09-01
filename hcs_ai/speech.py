from __future__ import annotations

import os
import queue
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path

from .config import ROOT


def natural_voice_asset_paths(root: Path = ROOT) -> tuple[Path, Path]:
    folder = Path(root) / "runtime" / "voice"
    return folder / "kokoro-v1.0.onnx", folder / "voices-v1.0.bin"


def natural_voice_ready(root: Path = ROOT) -> bool:
    return all(path.is_file() and path.stat().st_size > 0 for path in natural_voice_asset_paths(root))


def clean_for_speech(text: str) -> str:
    """Turn common chat Markdown into text that sounds natural when spoken."""
    value = str(text or "")
    value = re.sub(r"\x60\x60\x60.*?\x60\x60\x60", " Code omitted. ", value, flags=re.DOTALL)
    value = re.sub(r"!?(?:\[([^\]]+)\])\([^\)]+\)", r"\1", value)
    value = re.sub(r"\x60([^\x60]+)\x60", r"\1", value)
    value = value.replace("**", "").replace("__", "")
    value = re.sub(r"(?m)^\s{0,3}(?:#{1,6}\s+|[-*+]\s+|\d+[.)]\s+)", "", value)
    return re.sub(r"\s+", " ", value).strip()


def sentence_chunks(text: str, max_chars: int = 240) -> list[str]:
    """Group complete sentences into short chunks for lower-latency speech."""
    value = str(text or "").strip()
    if not value:
        return []
    sentences = re.findall(r".+?(?:[.!?](?=\s|$)|$)", value, flags=re.DOTALL)
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        sentence = re.sub(r"\s+", " ", sentence).strip()
        candidate = f"{current} {sentence}".strip()
        if current and len(candidate) > max_chars:
            chunks.append(current)
            current = sentence
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def native_speech_command() -> list[str] | None:
    """Return a local, OS-native text-to-speech command that reads stdin."""
    if os.name == "nt":
        shell = shutil.which("powershell.exe") or shutil.which("powershell")
        if not shell:
            return None
        script = (
            "Add-Type -AssemblyName System.Speech; "
            "$speaker = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            "$text = [Console]::In.ReadToEnd(); $speaker.Speak($text)"
        )
        return [shell, "-NoProfile", "-NonInteractive", "-Command", script]
    if sys.platform == "darwin":
        shell = shutil.which("say")
        return [shell] if shell else None
    shell = shutil.which("espeak-ng") or shutil.which("espeak")
    return [shell, "--stdin"] if shell else None


def run_speech_command(command: list[str], text: str) -> None:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    subprocess.run(
        command,
        input=text,
        text=True,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
    )


class SpeechRouter:
    """Choose the natural neural voice when ready, otherwise use native TTS."""

    def __init__(self, neural, native):
        self.neural = neural
        self.native = native

    @property
    def available(self) -> bool:
        return bool(self.neural.available or self.native.available)

    def speak(self, text: str) -> str:
        if self.neural.available:
            try:
                for chunk in sentence_chunks(text):
                    self.neural.speak(chunk)
                return "neural"
            except Exception:
                # Voice output must survive optional neural-runtime failures.
                pass
        self.native.speak(text)
        return "native"


class SpeechEngine:
    """Serialize spoken replies on a daemon worker so Tk never blocks."""

    def __init__(self, command: list[str] | None = None):
        self.command = list(command) if command is not None else native_speech_command()
        self._queue: queue.Queue[str] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        return bool(self.command)

    def speak(self, text: str) -> bool:
        spoken = clean_for_speech(text)
        if not spoken or not self.command:
            return False
        self._queue.put(spoken)
        with self._lock:
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._work, daemon=True)
                self._worker.start()
        return True

    def _work(self) -> None:
        while True:
            try:
                text = self._queue.get_nowait()
            except queue.Empty:
                return
            try:
                run_speech_command(self.command or [], text)
            finally:
                self._queue.task_done()
