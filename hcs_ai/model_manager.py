from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

import psutil

from .config import ROOT, load_config
from .telemetry import model_summary


MODELS_DIR = ROOT / "models"
_HF_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_QUANT = re.compile(r"(?:^|[-_.])(IQ\d(?:_[A-Z0-9]+)?|Q\d(?:_[A-Z0-9]+)+|Q\d)(?:[-_.]|$)", re.I)


def models_dir() -> Path:
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    return MODELS_DIR


def _meta_path(model_path: Path) -> Path:
    return model_path.with_suffix(model_path.suffix + ".hcs.json")


def _read_meta(model_path: Path) -> dict[str, Any]:
    try:
        return json.loads(_meta_path(model_path).read_text(encoding="utf-8"))
    except Exception:
        return {}


def quantization_from_name(name: str) -> str | None:
    match = _QUANT.search(name)
    return match.group(1).upper() if match else None


def _compatibility(size_bytes: int) -> dict[str, Any]:
    vm = psutil.virtual_memory()
    estimated = int(size_bytes * 1.25 + 512 * 1024 ** 2)
    return {
        "ram_total_bytes": int(vm.total),
        "ram_available_bytes": int(vm.available),
        "estimated_runtime_bytes": estimated,
        "likely_fits_total_ram": estimated <= int(vm.total * 0.85),
        "likely_fits_available_ram_now": estimated <= int(vm.available * 0.90),
    }


def list_local_models() -> list[dict[str, Any]]:
    active = Path(load_config().get("inference", {}).get("model_path") or "").expanduser()
    if active and not active.is_absolute():
        active = ROOT / active
    perf = {row["model"]: row for row in model_summary()}
    out: list[dict[str, Any]] = []
    for path in sorted(models_dir().rglob("*.gguf"), key=lambda p: p.name.lower()):
        try:
            stat = path.stat()
        except OSError:
            continue
        meta = _read_meta(path)
        entry = {
            "name": path.name,
            "path": str(path.resolve()),
            "size_bytes": stat.st_size,
            "format": "GGUF",
            "quantization": quantization_from_name(path.name),
            "active": bool(active) and path.resolve() == active.resolve(),
            "source_repo": meta.get("repo_id"),
            "source_file": meta.get("filename"),
            "sha256": meta.get("sha256"),
            "downloaded_at": meta.get("downloaded_at"),
            "compatibility": _compatibility(stat.st_size),
        }
        p = perf.get(str(path.resolve())) or perf.get(path.name)
        if p:
            entry["performance"] = p
        out.append(entry)
    return out


def search_huggingface(query: str, limit: int = 20) -> list[dict[str, Any]]:
    query = (query or "").strip()
    if not query:
        return []
    limit = max(1, min(int(limit), 50))
    params = urllib.parse.urlencode({
        "search": query,
        "filter": "gguf",
        "full": "true",
        "limit": str(limit),
    })
    req = urllib.request.Request(
        "https://huggingface.co/api/models?" + params,
        headers={"User-Agent": "HCS-AI/0.10 model-manager"},
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        repos = json.loads(response.read().decode("utf-8"))

    results: list[dict[str, Any]] = []
    for repo in repos if isinstance(repos, list) else []:
        repo_id = str(repo.get("id") or repo.get("modelId") or "")
        if not _HF_REPO.match(repo_id):
            continue
        siblings = repo.get("siblings") or []
        for sibling in siblings:
            filename = str(sibling.get("rfilename") or "")
            if not filename.lower().endswith(".gguf"):
                continue
            size = sibling.get("size")
            item = {
                "repo_id": repo_id,
                "filename": filename,
                "size_bytes": int(size) if isinstance(size, (int, float)) else None,
                "quantization": quantization_from_name(filename),
                "downloads": repo.get("downloads"),
                "likes": repo.get("likes"),
                "last_modified": repo.get("lastModified"),
                "license": (repo.get("cardData") or {}).get("license") if isinstance(repo.get("cardData"), dict) else None,
            }
            if item["size_bytes"]:
                item["compatibility"] = _compatibility(item["size_bytes"])
            results.append(item)
            if len(results) >= limit * 8:
                return results
    return results


def _validated_download(repo_id: str, filename: str) -> tuple[str, str]:
    repo_id = (repo_id or "").strip()
    filename = (filename or "").strip().replace("\\", "/")
    if not _HF_REPO.match(repo_id):
        raise ValueError("Invalid Hugging Face repository id.")
    if not filename.lower().endswith(".gguf"):
        raise ValueError("Only GGUF model files can be downloaded here.")
    parts = [part for part in filename.split("/") if part]
    if not parts or any(part in (".", "..") for part in parts):
        raise ValueError("Invalid model filename.")
    return repo_id, filename


def download_model(repo_id: str, filename: str,
                   progress: Callable[[int, int | None], None] | None = None) -> dict[str, Any]:
    repo_id, filename = _validated_download(repo_id, filename)
    target = models_dir() / Path(filename).name
    temp = target.with_suffix(target.suffix + ".part")
    encoded_name = "/".join(urllib.parse.quote(part, safe="") for part in filename.split("/"))
    url = f"https://huggingface.co/{repo_id}/resolve/main/{encoded_name}?download=true"
    req = urllib.request.Request(url, headers={"User-Agent": "HCS-AI/0.10 model-manager"})
    sha = hashlib.sha256()
    downloaded = 0
    total: int | None = None
    try:
        with urllib.request.urlopen(req, timeout=60) as response, temp.open("wb") as dst:
            header = response.headers.get("Content-Length")
            total = int(header) if header and header.isdigit() else None
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                dst.write(chunk)
                sha.update(chunk)
                downloaded += len(chunk)
                if progress:
                    progress(downloaded, total)
        os.replace(temp, target)
    except Exception:
        try:
            temp.unlink(missing_ok=True)
        except Exception:
            pass
        raise

    from .db import now_iso
    meta = {
        "repo_id": repo_id,
        "filename": filename,
        "source_url": url,
        "downloaded_at": now_iso(),
        "size_bytes": downloaded,
        "sha256": sha.hexdigest(),
    }
    _meta_path(target).write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return {"path": str(target.resolve()), **meta, "quantization": quantization_from_name(target.name)}


def delete_model(path_value: str) -> None:
    path = Path(path_value).expanduser().resolve()
    root = models_dir().resolve()
    if path.suffix.lower() != ".gguf" or root not in path.parents:
        raise ValueError("HCS only deletes GGUF files inside its managed models folder.")
    active = Path(load_config().get("inference", {}).get("model_path") or "").expanduser()
    if active and not active.is_absolute():
        active = ROOT / active
    if active and active.exists() and active.resolve() == path:
        raise RuntimeError("Stop or select a different model before deleting the active model.")
    path.unlink(missing_ok=False)
    _meta_path(path).unlink(missing_ok=True)


def import_local_model(source: str) -> dict[str, Any]:
    src = Path(source).expanduser().resolve()
    if not src.is_file() or src.suffix.lower() != ".gguf":
        raise ValueError("Choose an existing GGUF model file.")
    target = models_dir() / src.name
    if src != target.resolve():
        shutil.copy2(src, target)
    return {
        "path": str(target.resolve()),
        "name": target.name,
        "size_bytes": target.stat().st_size,
        "quantization": quantization_from_name(target.name),
    }
