from pathlib import Path
import re
from .db import connect
from .config import load_config

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
    with connect() as con:
        for p in files:
            if p.suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue
            try:
                if p.suffix.lower() == ".pdf":
                    from pypdf import PdfReader
                    reader = PdfReader(str(p))
                    text = "\n\n".join((page.extract_text() or "") for page in reader.pages)
                else:
                    text = p.read_text(encoding="utf-8", errors="ignore")
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
    return {"files_imported": imported, "chunks": chunks_total}

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
