from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from .config import ROOT


RUNTIME_DIR = ROOT / "runtime" / "llama.cpp"
SERVER_EXE = RUNTIME_DIR / "llama-server.exe"
PROVENANCE_PATH = RUNTIME_DIR / "install-provenance.json"
RELEASES_URL = "https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=30"
USER_AGENT = "HCS-AI-Installer"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _json_request(url: str):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _download(url: str, target: Path, progress=None) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as response, target.open("wb") as dst:
        total_header = response.headers.get("Content-Length")
        total = int(total_header) if total_header and total_header.isdigit() else None
        done = 0
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            dst.write(chunk)
            done += len(chunk)
            if progress:
                progress(done, total)


def find_runtime_asset() -> dict:
    releases = _json_request(RELEASES_URL)
    for release in releases if isinstance(releases, list) else []:
        if release.get("draft"):
            continue
        for asset in release.get("assets") or []:
            name = str(asset.get("name") or "")
            if name.startswith("llama-") and name.endswith("-bin-win-cpu-x64.zip"):
                return {
                    "release_tag": str(release.get("tag_name") or ""),
                    "asset_name": name,
                    "asset_url": str(asset.get("browser_download_url") or ""),
                }
    raise RuntimeError("No recent llama.cpp release contained a Windows CPU x64 package.")


def install_runtime(progress=None) -> dict:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    asset = find_runtime_asset()
    if not asset["asset_url"]:
        raise RuntimeError("GitHub returned the llama.cpp asset without a download URL.")

    with tempfile.TemporaryDirectory(prefix="hcs-llama-") as tmp:
        tmpdir = Path(tmp)
        archive = tmpdir / asset["asset_name"]
        extract = tmpdir / "extract"
        _download(asset["asset_url"], archive, progress=progress)

        if archive.stat().st_size < 1024 * 1024:
            raise RuntimeError("The downloaded llama.cpp archive is unexpectedly small and was rejected.")

        archive_sha256 = _sha256(archive)
        extract.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive, "r") as zf:
            zf.extractall(extract)

        candidates = list(extract.rglob("llama-server.exe"))
        if not candidates:
            raise RuntimeError("llama-server.exe was not found in the downloaded llama.cpp package.")

        source_dir = candidates[0].parent
        for child in source_dir.iterdir():
            dest = RUNTIME_DIR / child.name
            if child.is_dir():
                if dest.exists():
                    shutil.rmtree(dest)
                shutil.copytree(child, dest)
            else:
                shutil.copy2(child, dest)

    if not SERVER_EXE.is_file():
        raise RuntimeError("llama-server.exe was not present after installation.")

    record = {
        "source_repository": "ggml-org/llama.cpp",
        "release_tag": asset["release_tag"],
        "asset_name": asset["asset_name"],
        "asset_url": asset["asset_url"],
        "archive_sha256": archive_sha256,
        "llama_server_sha256": _sha256(SERVER_EXE),
        "llama_server_bytes": SERVER_EXE.stat().st_size,
        "llama_server_path": str(SERVER_EXE),
    }
    PROVENANCE_PATH.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return record


def existing_runtime_info() -> dict | None:
    if not SERVER_EXE.is_file():
        return None
    info = {
        "llama_server_sha256": _sha256(SERVER_EXE),
        "llama_server_bytes": SERVER_EXE.stat().st_size,
        "llama_server_path": str(SERVER_EXE),
    }
    try:
        saved = json.loads(PROVENANCE_PATH.read_text(encoding="utf-8"))
        if isinstance(saved, dict):
            info.update(saved)
    except Exception:
        pass
    return info