from pathlib import Path
import re
from .db import connect
from .config import load_config
from .knowledge_tree import register_file_artifact, classify_artifact

TEXT_EXTENSIONS = {".txt", ".md", ".py", ".json", ".csv", ".log", ".yaml", ".yml", ".toml"}
SUPPORTED_EXTENSIONS = TEXT_EXTENSIONS | {".pdf"}

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
    with connect() as con:
        for p in files:
            if p.suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue
            try:
                text = _read_supported_file(p)
            except Exception:
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

            try:
                artifact_id = register_file_artifact(p)
                classification = classify_artifact(
                    artifact_id=artifact_id,
                    title=p.name,
                    artifact_type=p.suffix.lower().lstrip(".") or "file",
                    text=text,
                    metadata={"path": str(p), "extension": p.suffix.lower()},
                )
                classified += 1
                artifacts.append({
                    "artifact_id": artifact_id,
                    "path": str(p),
                    "links": classification.get("linked", []),
                    "pending_review_node_ids": classification.get("pending_review_node_ids", []),
                })
            except Exception as exc:
                classification_errors.append({"path": str(p), "error": str(exc)})
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
