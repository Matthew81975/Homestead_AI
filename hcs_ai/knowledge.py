from pathlib import Path
import re
import time
import traceback

from .db import connect
from .config import ROOT, load_config
from .diagnostics import get_diagnostics
from .knowledge_tree import register_file_artifact, classify_artifact

TEXT_EXTENSIONS = {".txt", ".md", ".py", ".json", ".csv", ".log", ".yaml", ".yml", ".toml"}
SUPPORTED_EXTENSIONS = TEXT_EXTENSIONS | {".pdf"}
DIAGNOSTICS = get_diagnostics(ROOT / "data" / "logs")


def chunk_text(text: str, size: int, overlap: int):
    text = text.replace("\r\n", "\n")
    out = []
    start = 0
    while start < len(text):
        end = min(len(text), start + size)
        out.append(text[start:end])
        if end == len(text):
            break
        start = max(start + 1, end - overlap)
    return out


def _read_supported_file(path: Path) -> str:
    if path.suffix.lower() == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        return "\n\n".join((page.extract_text() or "") for page in reader.pages)
    return path.read_text(encoding="utf-8", errors="ignore")


def import_path(path_str: str) -> dict:
    path = Path(path_str).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(path)
    config = load_config()
    size = int(config["knowledge"]["chunk_chars"])
    overlap = int(config["knowledge"]["chunk_overlap"])
    files = [path] if path.is_file() else [
        p for p in path.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS
    ]
    imported = 0
    chunks_total = 0
    classified = 0
    classification_errors = []
    artifacts = []
    DIAGNOSTICS.emit(
        "INFO",
        "KnowledgeBase",
        "import",
        f"Importing {len(files)} candidate file(s)",
        diagnostic_payload={"path": str(path)},
    )
    with connect() as con:
        for p in files:
            if p.suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue
            try:
                text = _read_supported_file(p)
            except Exception as exc:
                DIAGNOSTICS.emit(
                    "WARNING",
                    "KnowledgeBase",
                    "read",
                    f"Could not read {p.name}: {exc}",
                    diagnostic_payload={"path": str(p)},
                    exception=traceback.format_exc(),
                )
                continue
            if not text.strip():
                continue
            con.execute("DELETE FROM kb_chunks WHERE source=?", (str(p),))
            chunks = chunk_text(text, size, overlap)
            for i, chunk in enumerate(chunks):
                con.execute(
                    "INSERT INTO kb_chunks(source, chunk_index, text) VALUES(?,?,?)",
                    (str(p), i, chunk),
                )
            imported += 1
            chunks_total += len(chunks)
            DIAGNOSTICS.emit(
                "INFO",
                "KnowledgeBase",
                "chunk",
                f"Chunked {p.name} into {len(chunks)} chunk(s)",
                context={"chunks": len(chunks), "characters": len(text)},
                diagnostic_payload={"path": str(p)},
            )

            started = time.perf_counter()
            try:
                artifact_id = register_file_artifact(p)
                DIAGNOSTICS.emit(
                    "INFO",
                    "KnowledgeBase",
                    "classification",
                    f"Classifying {p.name}",
                    context={"artifact_id": artifact_id, "characters": len(text)},
                    diagnostic_payload={"path": str(p)},
                )
                classification = classify_artifact(
                    artifact_id=artifact_id,
                    title=p.name,
                    artifact_type=p.suffix.lower().lstrip(".") or "file",
                    text=text,
                    metadata={"path": str(p), "extension": p.suffix.lower()},
                )
                elapsed = time.perf_counter() - started
                classified += 1
                links = classification.get("linked", [])
                artifacts.append({
                    "artifact_id": artifact_id,
                    "path": str(p),
                    "links": links,
                    "pending_review_node_ids": classification.get("pending_review_node_ids", []),
                })
                DIAGNOSTICS.emit(
                    "INFO",
                    "KnowledgeBase",
                    "classification",
                    f"Classified {p.name} into {len(links)} Knowledge Tree link(s)",
                    elapsed_seconds=elapsed,
                    context={"artifact_id": artifact_id, "links": len(links)},
                    diagnostic_payload={"path": str(p), "classification": classification},
                )
            except Exception as exc:
                elapsed = time.perf_counter() - started
                error = {"path": str(p), "error": str(exc)}
                classification_errors.append(error)
                DIAGNOSTICS.emit(
                    "ERROR",
                    "KnowledgeBase",
                    "classification",
                    f"Classification failed for {p.name}: {exc}",
                    elapsed_seconds=elapsed,
                    diagnostic_payload=error,
                    exception=traceback.format_exc(),
                )
    return {
        "files_imported": imported,
        "chunks": chunks_total,
        "artifacts_classified": classified,
        "classification_errors": classification_errors,
        "artifacts": artifacts,
    }


def _tokens(s: str):
    return set(re.findall(r"[A-Za-z0-9_]{2,}", s.lower()))


def search(query: str, limit: int = 6):
    q = _tokens(query)
    if not q:
        return []
    with connect() as con:
        rows = con.execute("SELECT source, chunk_index, text FROM kb_chunks").fetchall()
    scored = []
    for row in rows:
        t = _tokens(row["text"])
        score = len(q & t) / max(1, len(q))
        if score > 0:
            scored.append((score, dict(row)))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [{"score": round(s, 3), **r} for s, r in scored[:limit]]


def remove_source(path_str: str) -> dict:
    path = str(Path(path_str).expanduser().resolve())
    with connect() as con:
        cur = con.execute("DELETE FROM kb_chunks WHERE source=?", (path,))
    return {"ok": True, "source": path, "chunks_removed": cur.rowcount}


def active_sources() -> set[str]:
    with connect() as con:
        rows = con.execute("SELECT DISTINCT source FROM kb_chunks").fetchall()
    return {str(r[0]) for r in rows}
