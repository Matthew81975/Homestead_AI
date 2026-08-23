from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path

from .config import ROOT, load_config
from .db import audit


def homepage_path() -> Path:
    cfg = load_config().get("homepage", {})
    raw = cfg.get("path", "homepage/index.html")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    return path.resolve()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def status() -> dict:
    path = homepage_path()
    if not path.exists():
        return {"exists": False, "path": str(path)}
    data = path.read_bytes()
    st = path.stat()
    return {
        "exists": True,
        "path": str(path),
        "bytes": len(data),
        "sha256": _sha256_bytes(data),
        "modified_utc": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
    }


def read_source(args: dict | None = None) -> dict:
    args = args or {}
    path = homepage_path()
    if not path.exists():
        raise FileNotFoundError(f"Homepage source does not exist: {path}")
    max_bytes = min(max(int(args.get("max_bytes", 500_000)), 1_000), 2_000_000)
    data = path.read_bytes()
    shown = data[:max_bytes]
    return {
        **status(),
        "html": shown.decode("utf-8", errors="replace"),
        "truncated": len(data) > len(shown),
    }


def _check_expected(current: bytes, expected_sha256: str | None) -> None:
    if expected_sha256 and _sha256_bytes(current) != expected_sha256:
        raise RuntimeError(
            "Homepage changed since it was read. Read the source again before editing to avoid overwriting a newer change."
        )


def _validate_html(text: str) -> bytes:
    if "\x00" in text:
        raise ValueError("Homepage HTML contains a NUL byte.")
    data = text.encode("utf-8")
    cfg = load_config().get("homepage", {})
    max_bytes = int(cfg.get("max_source_bytes", 2_000_000))
    if len(data) > max_bytes:
        raise ValueError(f"Homepage source exceeds configured maximum of {max_bytes} bytes.")
    if "<html" not in text.lower() and "<!doctype" not in text.lower():
        raise ValueError("Replacement does not look like a complete HTML document.")
    return data


def _backup(current: bytes) -> str | None:
    if not current:
        return None
    cfg = load_config().get("homepage", {})
    if not cfg.get("backup_before_write", True):
        return None
    backup_dir = ROOT / "data" / "homepage_backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    short = _sha256_bytes(current)[:10]
    backup = backup_dir / f"index-{stamp}-{short}.html"
    backup.write_bytes(current)
    return str(backup)


def _atomic_write(new_data: bytes) -> dict:
    path = homepage_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    current = path.read_bytes() if path.exists() else b""
    backup = _backup(current)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_bytes(new_data)
    os.replace(temp, path)
    result = status()
    result["backup"] = backup
    audit("homepage_write", f"path={path} sha256={result.get('sha256')} backup={backup}")
    return result


def replace_source(args: dict) -> dict:
    if "html" not in args:
        raise ValueError("html is required")
    path = homepage_path()
    current = path.read_bytes() if path.exists() else b""
    _check_expected(current, args.get("expected_sha256"))
    new_data = _validate_html(str(args["html"]))
    if current == new_data:
        return {**status(), "changed": False, "backup": None}
    result = _atomic_write(new_data)
    result["changed"] = True
    return result


def patch_source(args: dict) -> dict:
    old_text = str(args.get("old_text", ""))
    new_text = str(args.get("new_text", ""))
    if not old_text:
        raise ValueError("old_text must be a non-empty exact substring from the current homepage source")
    path = homepage_path()
    current = path.read_bytes() if path.exists() else b""
    _check_expected(current, args.get("expected_sha256"))
    text = current.decode("utf-8")
    occurrences = text.count(old_text)
    if occurrences == 0:
        raise ValueError("old_text was not found in the current homepage source")
    if occurrences > 1 and not bool(args.get("replace_all", False)):
        raise ValueError(
            f"old_text occurs {occurrences} times. Supply a more specific substring or set replace_all=true intentionally."
        )
    updated = text.replace(old_text, new_text) if args.get("replace_all", False) else text.replace(old_text, new_text, 1)
    new_data = _validate_html(updated)
    result = _atomic_write(new_data)
    result.update({"changed": True, "replacements": occurrences if args.get("replace_all", False) else 1})
    return result
