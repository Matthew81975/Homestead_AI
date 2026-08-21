import csv
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import zipfile
import sys
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from .config import llm_config

import requests
import trafilatura
from ddgs import DDGS

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

APP_NAME = "HKR Builder v0.7 — Algorithm Library"
DEFAULT_ROOT = Path.home() / "Documents" / "Homestead_Knowledge_Repository"
SETTINGS_DIR = Path(os.environ.get("APPDATA", Path.home())) / "HKR_Builder"
SETTINGS_FILE = SETTINGS_DIR / "settings.json"
USER_AGENT = "HKR-Builder/0.7 (+personal offline knowledge repository)"
TIMEOUT = 25
MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024

# Default chosen to fit comfortably on a nominal 32 GB thumb drive.
DEFAULT_VOLUME_GB = 28.0

PREFERRED = {
    ".gov": 5,
    ".edu": 4,
    "extension.org": 4,
    "archive.org": 2,
}
BLOCKED_EXTENSIONS = {
    ".exe", ".msi", ".bat", ".cmd", ".com", ".scr", ".ps1",
    ".dll", ".jar", ".apk", ".dmg", ".pkg", ".iso"
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    object_id TEXT NOT NULL UNIQUE,
    sha256 TEXT NOT NULL UNIQUE,
    filename TEXT NOT NULL,
    original_title TEXT,
    source_url TEXT,
    final_url TEXT,
    source_domain TEXT,
    retrieved_utc TEXT NOT NULL,
    mime_type TEXT,
    bytes INTEGER NOT NULL,
    description TEXT,
    summary TEXT,
    structure_json TEXT,
    index_pointer_json TEXT,
    tags_json TEXT,
    source_score INTEGER DEFAULT 0,
    search_prompt TEXT,
    search_query TEXT,
    volume_name TEXT,
    volume_member TEXT
);
CREATE INDEX IF NOT EXISTS idx_documents_sha256 ON documents(sha256);
CREATE INDEX IF NOT EXISTS idx_documents_volume ON documents(volume_name);
CREATE INDEX IF NOT EXISTS idx_documents_domain ON documents(source_domain);

CREATE TABLE IF NOT EXISTS research_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_utc TEXT NOT NULL,
    prompt TEXT NOT NULL,
    queries_json TEXT,
    downloaded_count INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS software_resources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    resource_id TEXT NOT NULL UNIQUE,
    ecosystem TEXT NOT NULL,
    package_name TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    version TEXT NOT NULL,
    filename TEXT NOT NULL,
    package_type TEXT,
    python_requires TEXT,
    platform_tag TEXT,
    source_url TEXT NOT NULL,
    project_url TEXT,
    documentation_url TEXT,
    license TEXT,
    summary TEXT,
    requires_dist_json TEXT,
    sha256 TEXT NOT NULL,
    bytes INTEGER NOT NULL,
    retrieved_utc TEXT NOT NULL,
    install_hint TEXT,
    compatible INTEGER DEFAULT 0,
    metadata_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_software_package ON software_resources(normalized_name);
CREATE INDEX IF NOT EXISTS idx_software_version ON software_resources(normalized_name, version);
CREATE INDEX IF NOT EXISTS idx_software_sha256 ON software_resources(sha256);

CREATE TABLE IF NOT EXISTS algorithms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    algorithm_id TEXT NOT NULL UNIQUE, canonical_name TEXT NOT NULL, normalized_name TEXT NOT NULL,
    aliases_json TEXT, domain TEXT, problem_class TEXT, problem_solved TEXT, description TEXT,
    inputs_json TEXT, outputs_json TEXT, assumptions_json TEXT, constraints_json TEXT,
    time_complexity TEXT, space_complexity TEXT, hardware_notes TEXT, dependencies_json TEXT,
    pseudocode TEXT, reference_code TEXT, reference_language TEXT, tests_json TEXT, failure_modes_json TEXT,
    alternatives_json TEXT, license TEXT, confidence REAL DEFAULT 0.5, verification_status TEXT DEFAULT 'unverified',
    created_utc TEXT NOT NULL, updated_utc TEXT NOT NULL, metadata_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_algorithms_name ON algorithms(normalized_name);
CREATE INDEX IF NOT EXISTS idx_algorithms_domain ON algorithms(domain);
CREATE INDEX IF NOT EXISTS idx_algorithms_problem ON algorithms(problem_class);
CREATE TABLE IF NOT EXISTS algorithm_sources (
    algorithm_id TEXT NOT NULL, object_id TEXT NOT NULL, source_url TEXT, source_title TEXT, evidence TEXT, extracted_utc TEXT NOT NULL,
    PRIMARY KEY (algorithm_id, object_id)
);
CREATE INDEX IF NOT EXISTS idx_algorithm_sources_object ON algorithm_sources(object_id);
"""



def load_settings():
    """Load per-user HKR settings. Corrupt/missing settings fall back safely."""
    try:
        if SETTINGS_FILE.exists():
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}


def save_settings(settings):
    """Persist per-user HKR settings atomically enough for this desktop app."""
    try:
        SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
        tmp = SETTINGS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(settings, indent=2), encoding="utf-8")
        tmp.replace(SETTINGS_FILE)
        return True
    except Exception:
        return False


def utcnow():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def db_connect(root):
    root.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(root / "catalog.sqlite")
    db.executescript(SCHEMA)
    # Forward-compatible migration for repositories created by HKR <= v0.4.
    cols = {row[1] for row in db.execute("PRAGMA table_info(documents)")}
    for name, decl in (
        ("summary", "TEXT"),
        ("structure_json", "TEXT"),
        ("index_pointer_json", "TEXT"),
    ):
        if name not in cols:
            db.execute(f"ALTER TABLE documents ADD COLUMN {name} {decl}")
    db.commit()
    return db


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def safe_name(s, fallback="document"):
    s = re.sub(r"[^\w\-. ]+", "_", s or "", flags=re.UNICODE)
    s = re.sub(r"\s+", " ", s).strip(" ._")
    return (s[:120] or fallback)


def source_score(url):
    host = (urlparse(url).hostname or "").lower()
    score = 0
    for suffix, val in PREFERRED.items():
        if suffix.startswith("."):
            if host.endswith(suffix):
                score = max(score, val)
        elif host == suffix or host.endswith("." + suffix):
            score = max(score, val)
    return score


def lm_client():
    if OpenAI is None:
        return None
    try:
        return OpenAI(base_url=llm_config()["base_url"], api_key="hcs-local")
    except Exception:
        return None


def loaded_model(client):
    try:
        models = client.models.list()
        return models.data[0].id if models.data else None
    except Exception:
        return None


def llm_json(prompt, fallback, log, max_tokens=1200):
    client = lm_client()
    if not client:
        return fallback
    model = loaded_model(client)
    if not model:
        return fallback
    try:
        r = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.15,
            max_tokens=max(256, min(int(max_tokens), 8192)),
        )
        raw = r.choices[0].message.content.strip()
        # accept either JSON array or object
        a, o = raw.find("["), raw.find("{")
        starts = [x for x in (a, o) if x >= 0]
        if not starts:
            return fallback
        start = min(starts)
        end = max(raw.rfind("]"), raw.rfind("}"))
        return json.loads(raw[start:end+1])
    except Exception as e:
        log(f"LLM metadata/query fallback: {e}")
        return fallback


def make_queries(user_prompt, log):
    fallback = [
        user_prompt,
        f"{user_prompt} filetype:pdf",
        f"{user_prompt} manual guide university extension government",
        f"{user_prompt} technical reference troubleshooting",
    ]
    req = f"""
Act as the query planner for an offline homestead knowledge repository.
Create 4 to 6 concise web searches for authoritative, useful, downloadable
technical information responding to this request:

{user_prompt}

Favor government, university extension, manuals, technical guides, and public
educational material. Do not seek pirated copyrighted works or bypass paywalls.
Return ONLY a JSON array of strings.
"""
    out = llm_json(req, fallback, log)
    if isinstance(out, list):
        qs = [str(x).strip() for x in out if str(x).strip()]
        if qs:
            return qs[:6]
    return fallback


def classify_document(title, snippet, user_prompt, log):
    fallback = {
        "description": (snippet or title or "")[:500],
        "tags": []
    }
    req = f"""
You are cataloging a document for the Homestead Knowledge Repository.
Return ONLY JSON:
{{"description":"one concise sentence","tags":["tag1","tag2",...]}}
Use practical technical tags. Do not make claims beyond the supplied information.

Research goal: {user_prompt}
Title: {title}
Search snippet: {snippet}
"""
    out = llm_json(req, fallback, log)
    if not isinstance(out, dict):
        return fallback
    desc = str(out.get("description") or fallback["description"])[:800]
    tags = out.get("tags") or []
    if not isinstance(tags, list):
        tags = []
    tags = [str(t).strip().lower()[:80] for t in tags if str(t).strip()][:20]
    return {"description": desc, "tags": tags}



def extract_document_text(fetched, log):
    """Return (text, page_texts). page_texts is populated for PDFs."""
    kind = fetched.get("kind")
    raw = fetched.get("raw") or b""
    if kind in (".md", ".txt", ".html"):
        try:
            return raw.decode("utf-8", errors="replace"), []
        except Exception:
            return "", []
    if kind == ".pdf":
        try:
            import io
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(raw))
            pages = []
            for page in reader.pages:
                try:
                    pages.append(page.extract_text() or "")
                except Exception:
                    pages.append("")
            return "\n\n".join(pages), pages
        except Exception as e:
            log(f"PDF text extraction warning: {e}")
    return "", []


def detect_structure(text, page_texts=None):
    """Find compact navigational structure without copying a document index."""
    page_texts = page_texts or []
    chapter_titles = []
    seen = set()
    # Favor explicit chapter/section headings and short numbered headings.
    heading_re = re.compile(
        r"^(?:chapter\s+[\divxlcdm]+\b.*|part\s+[\divxlcdm]+\b.*|"
        r"section\s+\d+(?:\.\d+)*\b.*|\d+(?:\.\d+){0,3}\s+[A-Z][^.!?]{2,100})$",
        re.IGNORECASE,
    )
    for raw_line in text.splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not (3 <= len(line) <= 120):
            continue
        if heading_re.match(line):
            key = line.lower()
            if key not in seen:
                seen.add(key)
                chapter_titles.append(line)
            if len(chapter_titles) >= 80:
                break

    index_pointer = None
    if page_texts:
        # Search from the back because book indexes are normally near the end.
        for i in range(len(page_texts) - 1, max(-1, len(page_texts) // 2 - 1), -1):
            lines = [x.strip() for x in page_texts[i].splitlines() if x.strip()]
            if any(re.fullmatch(r"index", x, flags=re.IGNORECASE) for x in lines[:30]):
                index_pointer = {"type": "page", "page": i + 1}
                break
    else:
        for lineno, raw_line in enumerate(text.splitlines(), 1):
            if re.fullmatch(r"\s*(?:#+\s*)?index\s*", raw_line, flags=re.IGNORECASE):
                index_pointer = {"type": "line", "line": lineno}
                break

    return {
        "chapter_titles": chapter_titles,
        "index_pointer": index_pointer,
    }


def summarize_document_content(title, text, structure, snippet, user_prompt, log):
    """Create a compact semantic summary used for routing/retrieval."""
    fallback = (snippet or title or "Document")[:1000]
    if not text.strip():
        return fallback

    # Local models vary widely in context size. Keep the request bounded and
    # sample beginning/middle/end so long manuals remain usable.
    clean = re.sub(r"\n{3,}", "\n\n", text).strip()
    limit = 24000
    if len(clean) > limit:
        third = limit // 3
        mid = max(0, len(clean) // 2 - third // 2)
        sample = clean[:third] + "\n\n[...middle sample...]\n\n" + clean[mid:mid+third] + \
                 "\n\n[...ending sample...]\n\n" + clean[-third:]
    else:
        sample = clean

    chapters = structure.get("chapter_titles") or []
    req = f"""
You are creating a compact navigation summary for an offline technical library.
Summarize what this document covers, its purpose, and the kinds of questions it
can answer. Be factual and concise. Do NOT reproduce an index. If chapter titles
are supplied, use them only to understand scope. Return ONLY JSON:
{{"summary":"2 to 6 concise sentences"}}

Library research goal: {user_prompt}
Title: {title}
Detected chapter/section titles: {json.dumps(chapters[:40], ensure_ascii=False)}
Document sample:\n{sample}
"""
    out = llm_json(req, {"summary": fallback}, log)
    if isinstance(out, dict) and str(out.get("summary") or "").strip():
        return str(out["summary"]).strip()[:4000]
    return fallback


def build_library_metadata(fetched, user_prompt, log):
    text, pages = extract_document_text(fetched, log)
    structure = detect_structure(text, pages)
    summary = summarize_document_content(
        fetched.get("title") or "", text, structure,
        fetched.get("snippet") or "", user_prompt, log
    )
    return {
        "summary": summary,
        "chapter_titles": structure.get("chapter_titles") or [],
        # Pointer only; never copy the source index into metadata.
        "index_pointer": structure.get("index_pointer"),
    }

def search(queries, per_query, log):
    seen = {}
    ddgs = DDGS()
    for q in queries:
        log(f"Searching: {q}")
        try:
            for r in ddgs.text(q, max_results=per_query):
                url = (r.get("href") or r.get("url") or "").strip()
                if not url.startswith(("https://", "http://")):
                    continue
                if url not in seen:
                    seen[url] = {
                        "url": url,
                        "title": r.get("title") or "",
                        "snippet": r.get("body") or r.get("snippet") or "",
                        "query": q,
                        "score": source_score(url),
                    }
        except Exception as e:
            log(f"Search warning: {e}")
        time.sleep(0.25)
    vals = list(seen.values())
    vals.sort(
        key=lambda x: (x["score"], x["url"].lower().endswith(".pdf")),
        reverse=True
    )
    return vals


def detect_kind(url, content_type):
    ext = Path(urlparse(url).path).suffix.lower()
    if ext in BLOCKED_EXTENSIONS:
        return "blocked"
    if "application/pdf" in content_type or ext == ".pdf":
        return ".pdf"
    if "text/plain" in content_type:
        return ".txt"
    if "text/html" in content_type or "application/xhtml" in content_type or not ext:
        return ".html"
    return None


def fetch_candidate(item, log):
    try:
        with requests.get(
            item["url"], headers={"User-Agent": USER_AGENT},
            timeout=TIMEOUT, stream=True, allow_redirects=True
        ) as r:
            r.raise_for_status()
            ctype = (r.headers.get("content-type") or "").lower()
            kind = detect_kind(r.url, ctype)
            if kind == "blocked":
                return None, "blocked file type"
            if kind is None:
                return None, f"unsupported type {ctype or 'unknown'}"
            declared = r.headers.get("content-length")
            if declared and int(declared) > MAX_DOWNLOAD_BYTES:
                return None, "too large"

            chunks, total = [], 0
            for chunk in r.iter_content(128 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    return None, "exceeded download limit"
                chunks.append(chunk)
            raw = b"".join(chunks)

            title = item["title"] or Path(urlparse(r.url).path).stem or "document"

            if kind == ".html":
                html = raw.decode(r.encoding or "utf-8", errors="replace")
                text = trafilatura.extract(
                    html, url=r.url, output_format="markdown",
                    include_links=True, include_tables=True, favor_precision=True
                )
                if not text or len(text.strip()) < 400:
                    return None, "too little useful page text"
                raw = (
                    f"# {title}\n\nSource: {r.url}\nRetrieved: {utcnow()}\n\n{text.strip()}\n"
                ).encode("utf-8")
                kind = ".md"

            return {
                **item,
                "final_url": r.url,
                "content_type": ctype,
                "kind": kind,
                "raw": raw,
                "title": title,
            }, None
    except Exception as e:
        return None, str(e)


def store_document(root, db, fetched, user_prompt, log):
    digest = sha256_bytes(fetched["raw"])
    existing = db.execute(
        "SELECT object_id FROM documents WHERE sha256=?", (digest,)
    ).fetchone()
    if existing:
        return None, f"duplicate of {existing[0]}"

    # IDs are content-derived, portable, and independent of folder organization.
    object_id = "HKR-" + digest[:16].upper()
    objects = root / "objects"
    objects.mkdir(exist_ok=True)
    filename = f"{object_id}{fetched['kind']}"
    path = objects / filename
    path.write_bytes(fetched["raw"])

    meta = classify_document(
        fetched["title"], fetched["snippet"], user_prompt, log
    )
    nav = build_library_metadata(fetched, user_prompt, log)
    domain = (urlparse(fetched["final_url"]).hostname or "").lower()

    structure_json = json.dumps(
        {"chapter_titles": nav["chapter_titles"]}, ensure_ascii=False
    )
    index_pointer_json = json.dumps(nav["index_pointer"], ensure_ascii=False) \
        if nav["index_pointer"] else None

    db.execute("""
        INSERT INTO documents (
            object_id, sha256, filename, original_title, source_url, final_url,
            source_domain, retrieved_utc, mime_type, bytes, description, summary,
            structure_json, index_pointer_json, tags_json, source_score,
            search_prompt, search_query
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        object_id, digest, filename, fetched["title"], fetched["url"],
        fetched["final_url"], domain, utcnow(), fetched["content_type"],
        len(fetched["raw"]), meta["description"], nav["summary"],
        structure_json, index_pointer_json,
        json.dumps(meta["tags"], ensure_ascii=False),
        fetched["score"], user_prompt, fetched["query"]
    ))
    db.commit()
    # Best-effort algorithm harvesting: failure never blocks document ingestion.
    try:
        found = extract_algorithms_from_document(root, db, object_id, log)
        if found:
            log(f"Extracted/linked {len(found)} algorithm record(s) from {object_id}")
    except Exception as e:
        log(f"Algorithm extraction warning for {object_id}: {e}")
    return object_id, None




def normalize_algorithm_name(name):
    return re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")


def _json_list(value):
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    if value is None:
        return []
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _algorithm_fingerprint(item):
    base = "|".join([
        normalize_algorithm_name(item.get("name") or item.get("canonical_name") or "algorithm"),
        normalize_algorithm_name(item.get("problem_class") or ""),
        normalize_algorithm_name(item.get("problem_solved") or "")[:80],
    ])
    return hashlib.sha256(base.encode("utf-8", errors="ignore")).hexdigest()[:16].upper()


def extract_algorithm_candidates(title, text, source_url="", log=lambda x: None):
    """Use the configured local LLM to identify reusable algorithms in source text."""
    clean = re.sub(r"\n{3,}", "\n\n", text or "").strip()
    if len(clean) < 250:
        return []
    # Bound context while sampling the full document. Extraction is conservative:
    # only named or clearly specified reusable computational procedures qualify.
    limit = 30000
    if len(clean) > limit:
        third = limit // 3
        mid = max(0, len(clean)//2 - third//2)
        sample = clean[:third] + "\n\n[...middle...]\n\n" + clean[mid:mid+third] + "\n\n[...end...]\n\n" + clean[-third:]
    else:
        sample = clean
    fallback = []
    prompt = f"""
You are extracting reusable algorithms for the Homestead Knowledge Repository (HKR).
Identify ONLY genuine reusable computational algorithms, numerical methods, control methods,
search/planning methods, signal/image/audio processing procedures, data structures with an
algorithmic procedure, or clearly specified stepwise computational techniques in the source.
Do not invent algorithms and do not treat ordinary prose instructions as algorithms.

Return ONLY a JSON array. Each item must use this schema:
{{
  "name":"canonical algorithm name",
  "aliases":["other names"],
  "domain":"short domain",
  "problem_class":"short problem class",
  "problem_solved":"what it solves",
  "description":"2-5 factual sentences",
  "inputs":["inputs"],
  "outputs":["outputs"],
  "assumptions":["assumptions"],
  "constraints":["constraints"],
  "time_complexity":"Big-O or unknown",
  "space_complexity":"Big-O or unknown",
  "hardware_notes":"hardware/numerical notes or empty",
  "dependencies":["conceptual/software dependencies"],
  "pseudocode":"compact pseudocode if the source supports it, otherwise empty",
  "reference_code":"short source-supported code only, otherwise empty",
  "reference_language":"language or empty",
  "tests":["test/example ideas"],
  "failure_modes":["known failure modes"],
  "alternatives":["related alternatives"],
  "license":"license if explicitly known, otherwise empty",
  "confidence":0.0,
  "evidence":"brief source-grounded reason this is an algorithm"
}}
Confidence is 0 to 1. Prefer an empty array to a speculative extraction.

Source title: {title}
Source URL: {source_url}
Source text:\n{sample}
"""
    out = llm_json(prompt, fallback, log, max_tokens=5000)
    if not isinstance(out, list):
        return []
    result=[]
    for item in out[:40]:
        if not isinstance(item, dict):
            continue
        name=str(item.get("name") or "").strip()
        problem=str(item.get("problem_solved") or "").strip()
        try: confidence=float(item.get("confidence",0.5))
        except Exception: confidence=0.5
        if not name or not problem or confidence < 0.35:
            continue
        item["confidence"] = max(0.0, min(1.0, confidence))
        result.append(item)
    return result


def upsert_algorithm(root, db, item, source=None):
    """Insert/merge one canonical algorithm and link it to its provenance."""
    name=str(item.get("name") or item.get("canonical_name") or "").strip()
    if not name:
        return None
    norm=normalize_algorithm_name(name)
    aliases=_json_list(item.get("aliases"))
    # Deduplicate primarily by normalized canonical name or alias. If no match,
    # create a stable semantic ID from name/problem identity.
    row=db.execute("SELECT algorithm_id, aliases_json, confidence, verification_status FROM algorithms WHERE normalized_name=?",(norm,)).fetchone()
    if not row:
        # Find canonical record whose aliases already contain this normalized name.
        for candidate in db.execute("SELECT algorithm_id, aliases_json, confidence, verification_status FROM algorithms"):
            cand_aliases=[normalize_algorithm_name(x) for x in json.loads(candidate[1] or "[]")]
            if norm in cand_aliases:
                row=candidate; break
    if row:
        aid=row[0]
        old_aliases=json.loads(row[1] or "[]")
        merged=[]
        seen=set()
        for a in old_aliases+aliases:
            k=normalize_algorithm_name(a)
            if k and k != norm and k not in seen:
                seen.add(k); merged.append(a)
        old_conf=float(row[2] or 0.0)
        new_conf=max(old_conf,float(item.get("confidence") or 0.0))
        db.execute("""
            UPDATE algorithms SET aliases_json=?, domain=COALESCE(NULLIF(?,''),domain),
              problem_class=COALESCE(NULLIF(?,''),problem_class), problem_solved=COALESCE(NULLIF(?,''),problem_solved),
              description=CASE WHEN length(COALESCE(?,'')) > length(COALESCE(description,'')) THEN ? ELSE description END,
              inputs_json=CASE WHEN ?!='[]' THEN ? ELSE inputs_json END,
              outputs_json=CASE WHEN ?!='[]' THEN ? ELSE outputs_json END,
              assumptions_json=CASE WHEN ?!='[]' THEN ? ELSE assumptions_json END,
              constraints_json=CASE WHEN ?!='[]' THEN ? ELSE constraints_json END,
              time_complexity=COALESCE(NULLIF(?,''),time_complexity), space_complexity=COALESCE(NULLIF(?,''),space_complexity),
              hardware_notes=COALESCE(NULLIF(?,''),hardware_notes), dependencies_json=CASE WHEN ?!='[]' THEN ? ELSE dependencies_json END,
              pseudocode=CASE WHEN length(COALESCE(?,'')) > length(COALESCE(pseudocode,'')) THEN ? ELSE pseudocode END,
              reference_code=CASE WHEN length(COALESCE(?,'')) > length(COALESCE(reference_code,'')) THEN ? ELSE reference_code END,
              reference_language=COALESCE(NULLIF(?,''),reference_language), tests_json=CASE WHEN ?!='[]' THEN ? ELSE tests_json END,
              failure_modes_json=CASE WHEN ?!='[]' THEN ? ELSE failure_modes_json END,
              alternatives_json=CASE WHEN ?!='[]' THEN ? ELSE alternatives_json END,
              license=COALESCE(NULLIF(?,''),license), confidence=?, updated_utc=?
            WHERE algorithm_id=?
        """,(
            json.dumps(merged,ensure_ascii=False), str(item.get("domain") or ""), str(item.get("problem_class") or ""),
            str(item.get("problem_solved") or ""), str(item.get("description") or ""), str(item.get("description") or ""),
            json.dumps(_json_list(item.get("inputs")),ensure_ascii=False), json.dumps(_json_list(item.get("inputs")),ensure_ascii=False),
            json.dumps(_json_list(item.get("outputs")),ensure_ascii=False), json.dumps(_json_list(item.get("outputs")),ensure_ascii=False),
            json.dumps(_json_list(item.get("assumptions")),ensure_ascii=False), json.dumps(_json_list(item.get("assumptions")),ensure_ascii=False),
            json.dumps(_json_list(item.get("constraints")),ensure_ascii=False), json.dumps(_json_list(item.get("constraints")),ensure_ascii=False),
            str(item.get("time_complexity") or ""), str(item.get("space_complexity") or ""), str(item.get("hardware_notes") or ""),
            json.dumps(_json_list(item.get("dependencies")),ensure_ascii=False), json.dumps(_json_list(item.get("dependencies")),ensure_ascii=False),
            str(item.get("pseudocode") or ""), str(item.get("pseudocode") or ""), str(item.get("reference_code") or ""), str(item.get("reference_code") or ""),
            str(item.get("reference_language") or ""), json.dumps(_json_list(item.get("tests")),ensure_ascii=False), json.dumps(_json_list(item.get("tests")),ensure_ascii=False),
            json.dumps(_json_list(item.get("failure_modes")),ensure_ascii=False), json.dumps(_json_list(item.get("failure_modes")),ensure_ascii=False),
            json.dumps(_json_list(item.get("alternatives")),ensure_ascii=False), json.dumps(_json_list(item.get("alternatives")),ensure_ascii=False),
            str(item.get("license") or ""), new_conf, utcnow(), aid
        ))
    else:
        aid="ALG-"+_algorithm_fingerprint(item)
        # Handle the rare semantic hash collision deterministically.
        if db.execute("SELECT 1 FROM algorithms WHERE algorithm_id=?",(aid,)).fetchone():
            aid="ALG-"+hashlib.sha256((aid+name+utcnow()).encode()).hexdigest()[:16].upper()
        db.execute("""
            INSERT INTO algorithms (algorithm_id,canonical_name,normalized_name,aliases_json,domain,problem_class,
              problem_solved,description,inputs_json,outputs_json,assumptions_json,constraints_json,time_complexity,
              space_complexity,hardware_notes,dependencies_json,pseudocode,reference_code,reference_language,tests_json,
              failure_modes_json,alternatives_json,license,confidence,verification_status,created_utc,updated_utc,metadata_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,(
            aid,name,norm,json.dumps(aliases,ensure_ascii=False),str(item.get("domain") or ""),str(item.get("problem_class") or ""),
            str(item.get("problem_solved") or ""),str(item.get("description") or ""),json.dumps(_json_list(item.get("inputs")),ensure_ascii=False),
            json.dumps(_json_list(item.get("outputs")),ensure_ascii=False),json.dumps(_json_list(item.get("assumptions")),ensure_ascii=False),
            json.dumps(_json_list(item.get("constraints")),ensure_ascii=False),str(item.get("time_complexity") or ""),str(item.get("space_complexity") or ""),
            str(item.get("hardware_notes") or ""),json.dumps(_json_list(item.get("dependencies")),ensure_ascii=False),str(item.get("pseudocode") or ""),
            str(item.get("reference_code") or ""),str(item.get("reference_language") or ""),json.dumps(_json_list(item.get("tests")),ensure_ascii=False),
            json.dumps(_json_list(item.get("failure_modes")),ensure_ascii=False),json.dumps(_json_list(item.get("alternatives")),ensure_ascii=False),
            str(item.get("license") or ""),float(item.get("confidence") or 0.5),"unverified",utcnow(),utcnow(),"{}"
        ))
    if source and source.get("object_id"):
        db.execute("""
            INSERT OR REPLACE INTO algorithm_sources(algorithm_id,object_id,source_url,source_title,evidence,extracted_utc)
            VALUES(?,?,?,?,?,?)
        """,(aid,source.get("object_id"),source.get("source_url") or "",source.get("source_title") or "",str(item.get("evidence") or "")[:1500],utcnow()))
    db.commit()
    return aid


def extract_algorithms_from_document(root, db, object_id, log=lambda x: None):
    row=db.execute("SELECT filename,original_title,source_url FROM documents WHERE object_id=?",(object_id,)).fetchone()
    if not row:
        return []
    path=root/"objects"/row[0]
    if not path.exists():
        return []
    try:
        raw=path.read_bytes()
        fetched={"kind":path.suffix.lower(),"raw":raw}
        text,_=extract_document_text(fetched,log)
    except Exception as e:
        log(f"Algorithm scan skipped {object_id}: {e}"); return []
    candidates=extract_algorithm_candidates(row[1] or row[0],text,row[2] or "",log)
    added=[]
    source={"object_id":object_id,"source_url":row[2] or "","source_title":row[1] or row[0]}
    for item in candidates:
        aid=upsert_algorithm(root,db,item,source)
        if aid: added.append(aid)
    return added


def scan_algorithms(root_path, object_ids=None, limit=500, log=lambda x: None):
    root=Path(root_path).expanduser(); db=db_connect(root)
    try:
        if object_ids:
            ids=[str(x) for x in object_ids]
        else:
            ids=[r[0] for r in db.execute("SELECT object_id FROM documents ORDER BY id DESC LIMIT ?",(max(1,min(int(limit),5000)),))]
        total=0; unique=set()
        for i,oid in enumerate(ids,1):
            log(f"Scanning algorithms {i}/{len(ids)}: {oid}")
            found=extract_algorithms_from_document(root,db,oid,log)
            total += len(found); unique.update(found)
        return {"documents_scanned":len(ids),"extractions":total,"unique_algorithms":len(unique)}
    finally:
        db.close()


def _algorithm_row_to_dict(row, cols):
    d=dict(zip(cols,row))
    for key in ("aliases_json","inputs_json","outputs_json","assumptions_json","constraints_json","dependencies_json","tests_json","failure_modes_json","alternatives_json"):
        outkey=key[:-5]
        try: d[outkey]=json.loads(d.pop(key) or "[]")
        except Exception: d[outkey]=[]; d.pop(key,None)
    return d


def list_algorithms(root_path, query="", domain="", limit=1000):
    root=Path(root_path).expanduser(); db=db_connect(root)
    cols=["algorithm_id","canonical_name","aliases_json","domain","problem_class","problem_solved","description","inputs_json","outputs_json",
          "assumptions_json","constraints_json","time_complexity","space_complexity","hardware_notes","dependencies_json","pseudocode","reference_code",
          "reference_language","tests_json","failure_modes_json","alternatives_json","license","confidence","verification_status","created_utc","updated_utc"]
    try:
        sql="SELECT "+",".join(cols)+" FROM algorithms WHERE 1=1"; args=[]
        q=(query or "").strip().lower()
        if q:
            terms=[t for t in re.findall(r"[a-zA-Z0-9_+.-]+",q) if len(t)>1][:12]
            if terms:
                clauses=[]
                for t in terms:
                    clauses.append("(lower(canonical_name) LIKE ? OR lower(aliases_json) LIKE ? OR lower(domain) LIKE ? OR lower(problem_class) LIKE ? OR lower(problem_solved) LIKE ? OR lower(description) LIKE ? OR lower(constraints_json) LIKE ? OR lower(alternatives_json) LIKE ?)")
                    args.extend([f"%{t}%"]*8)
                sql += " AND ("+" OR ".join(clauses)+")"
        if domain:
            sql += " AND lower(domain) LIKE ?"; args.append(f"%{domain.lower()}%")
        sql += " ORDER BY confidence DESC, canonical_name COLLATE NOCASE LIMIT ?"; args.append(max(1,min(int(limit),5000)))
        rows=db.execute(sql,args).fetchall()
        return [_algorithm_row_to_dict(r,cols) for r in rows]
    finally: db.close()


def get_algorithm(root_path, algorithm_id):
    root=Path(root_path).expanduser(); db=db_connect(root)
    cols=["algorithm_id","canonical_name","aliases_json","domain","problem_class","problem_solved","description","inputs_json","outputs_json",
          "assumptions_json","constraints_json","time_complexity","space_complexity","hardware_notes","dependencies_json","pseudocode","reference_code",
          "reference_language","tests_json","failure_modes_json","alternatives_json","license","confidence","verification_status","created_utc","updated_utc"]
    try:
        row=db.execute("SELECT "+",".join(cols)+" FROM algorithms WHERE algorithm_id=?",(algorithm_id,)).fetchone()
        if not row: return None
        d=_algorithm_row_to_dict(row,cols)
        src_cols=["object_id","source_url","source_title","evidence","extracted_utc"]
        d["sources"]=[dict(zip(src_cols,r)) for r in db.execute("SELECT "+",".join(src_cols)+" FROM algorithm_sources WHERE algorithm_id=? ORDER BY extracted_utc DESC",(algorithm_id,))]
        return d
    finally: db.close()


def generate_algorithm_code(root_path, algorithm_id, language="python", requirements="", log=lambda x: None):
    alg=get_algorithm(root_path,algorithm_id)
    if not alg: raise ValueError("Algorithm not found")
    fallback={"code":alg.get("reference_code") or alg.get("pseudocode") or "","notes":"Local LLM unavailable; returning stored reference/pseudocode.","tests":[]}
    prompt=f"""
You are the coding layer for HKR capable knowledge. Implement the algorithm below in {language}.
Respect the stated assumptions and failure modes. Prefer a clean reusable function/class, validate inputs,
and include concise executable tests or examples. Do not silently change the algorithm. If a requirement
conflicts with the algorithm, explain it in notes. Return ONLY JSON:
{{"code":"complete implementation","notes":"important tradeoffs/assumptions","tests":["test descriptions"]}}

User deployment requirements: {requirements or 'none supplied'}
Algorithm record:\n{json.dumps(alg,ensure_ascii=False)[:26000]}
"""
    out=llm_json(prompt,fallback,log,max_tokens=5000)
    return out if isinstance(out,dict) else fallback

def normalize_package_name(name):
    return re.sub(r"[-_.]+", "-", (name or "").strip()).lower()


def pypi_package_info(package_name):
    """Fetch package metadata from PyPI without downloading or executing package code."""
    name = normalize_package_name(package_name)
    if not name:
        raise ValueError("Package name is required")
    url = f"https://pypi.org/pypi/{name}/json"
    r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
    if r.status_code == 404:
        raise ValueError(f"Python package not found on PyPI: {package_name}")
    r.raise_for_status()
    data = r.json()
    info = data.get("info") or {}
    urls = data.get("urls") or []
    return {
        "ecosystem": "pypi",
        "name": info.get("name") or package_name,
        "normalized_name": name,
        "version": info.get("version") or "",
        "summary": info.get("summary") or "",
        "license": info.get("license") or "",
        "project_url": info.get("project_url") or info.get("package_url") or f"https://pypi.org/project/{name}/",
        "documentation_url": (info.get("project_urls") or {}).get("Documentation"),
        "requires_python": info.get("requires_python") or "",
        "requires_dist": info.get("requires_dist") or [],
        "files": urls,
    }


def _compatible_wheel_candidates(files):
    """Return wheel files ordered by compatibility with this running Python."""
    try:
        from packaging.tags import sys_tags
        from packaging.utils import parse_wheel_filename
        supported = list(sys_tags())
        rank = {tag: i for i, tag in enumerate(supported)}
        matches = []
        for f in files:
            fn = f.get("filename") or ""
            if f.get("packagetype") != "bdist_wheel" or not fn.endswith(".whl"):
                continue
            try:
                _, _, _, tags = parse_wheel_filename(fn)
            except Exception:
                continue
            best = min((rank[t] for t in tags if t in rank), default=None)
            if best is not None:
                matches.append((best, f))
        matches.sort(key=lambda x: x[0])
        return [f for _, f in matches]
    except Exception:
        return []


def choose_python_distribution(package_meta, prefer_binary=True):
    files = package_meta.get("files") or []
    if prefer_binary:
        wheels = _compatible_wheel_candidates(files)
        if wheels:
            out = dict(wheels[0])
            out["compatible"] = True
            return out
    # A source distribution can be archived even when it cannot be installed on this machine.
    sdists = [f for f in files if f.get("packagetype") == "sdist"]
    if sdists:
        out = dict(sdists[0])
        out["compatible"] = False
        return out
    # Last resort: retain any published distribution, but mark it non-compatible.
    if files:
        out = dict(files[0])
        out["compatible"] = False
        return out
    raise ValueError(f"No downloadable distributions published for {package_meta.get('name')}")


def cache_python_package(root_path, package_name, prefer_binary=True):
    """Cache one PyPI distribution in HKR. This never installs or imports the package."""
    root = Path(root_path).expanduser()
    db = db_connect(root)
    try:
        meta = pypi_package_info(package_name)
        dist = choose_python_distribution(meta, prefer_binary=prefer_binary)
        url = dist.get("url")
        if not url:
            raise ValueError("Selected PyPI distribution has no download URL")
        size = int(dist.get("size") or 0)
        if size and size > MAX_DOWNLOAD_BYTES * 20:
            raise ValueError(f"Package archive is too large for the HKR software cache ({size} bytes)")
        normalized=meta["normalized_name"]
        version=meta["version"]
        filename=safe_name(dist.get("filename") or f"{normalized}-{version}.pkg", "package.pkg")
        dest=root/"software"/"python"/normalized/version
        dest.mkdir(parents=True, exist_ok=True)
        path=dest/filename
        tmp=path.with_suffix(path.suffix + ".part")
        digestor=hashlib.sha256(); total=0
        try:
            with requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT, stream=True) as r:
                r.raise_for_status()
                hard_cap = 2 * 1024 * 1024 * 1024
                with tmp.open("wb") as f:
                    for chunk in r.iter_content(1024 * 1024):
                        if not chunk: continue
                        total += len(chunk)
                        if total > hard_cap:
                            raise ValueError("Package archive exceeded 2 GB safety cap")
                        digestor.update(chunk); f.write(chunk)
            digest=digestor.hexdigest()
            expected=((dist.get("digests") or {}).get("sha256") or "").lower()
            if expected and digest.lower() != expected:
                raise ValueError("SHA-256 mismatch; package was not cached")
            tmp.replace(path)
        finally:
            if tmp.exists(): tmp.unlink()
        resource_id="HKR-PY-"+digest[:16].upper()
        install_hint=f'python -m pip install "{path}"'
        db.execute("""
            INSERT OR REPLACE INTO software_resources (
                resource_id, ecosystem, package_name, normalized_name, version, filename,
                package_type, python_requires, platform_tag, source_url, project_url,
                documentation_url, license, summary, requires_dist_json, sha256, bytes,
                retrieved_utc, install_hint, compatible, metadata_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            resource_id, "pypi", meta["name"], normalized, version, filename,
            dist.get("packagetype"), meta.get("requires_python"), dist.get("python_version"),
            url, meta.get("project_url"), meta.get("documentation_url"), meta.get("license"),
            meta.get("summary"), json.dumps(meta.get("requires_dist") or []), digest, total,
            utcnow(), install_hint, int(bool(dist.get("compatible"))),
            json.dumps({"dist": dist}, ensure_ascii=False)
        ))
        db.commit()
        return {
            "resource_id": resource_id, "name": meta["name"], "version": version,
            "filename": filename, "path": str(path), "bytes": total,
            "sha256": digest, "compatible": bool(dist.get("compatible")),
            "package_type": dist.get("packagetype"), "python_requires": meta.get("requires_python"),
            "install_hint": install_hint, "installed": False,
        }
    finally:
        db.close()



def _distribution_identity(filename):
    try:
        from packaging.utils import parse_wheel_filename, parse_sdist_filename
        if filename.endswith(".whl"):
            name, version, _, _ = parse_wheel_filename(filename)
            return str(name), str(version), "bdist_wheel"
        name, version = parse_sdist_filename(filename)
        return str(name), str(version), "sdist"
    except Exception:
        stem=filename.split("-")[0] if "-" in filename else filename
        return stem, "unknown", "archive"


def cache_python_bundle(root_path, package_name, prefer_binary=True):
    """Use pip download to resolve and cache a package plus dependencies, without installing them."""
    root=Path(root_path).expanduser()
    requested=normalize_package_name(package_name)
    if not requested:
        raise ValueError("Package name is required")
    bundle=root/"software"/"python"/"bundles"/safe_name(requested, "package")
    bundle.mkdir(parents=True, exist_ok=True)
    cmd=[sys.executable, "-m", "pip", "download", package_name, "--dest", str(bundle), "--disable-pip-version-check"]
    if prefer_binary:
        cmd.append("--only-binary=:all:")
    proc=subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "pip download failed")[-4000:])
    files=[]
    db=db_connect(root)
    try:
        for path in sorted(bundle.iterdir()):
            if not path.is_file(): continue
            digest=hashlib.sha256(path.read_bytes()).hexdigest()
            name, version, ptype=_distribution_identity(path.name)
            rid="HKR-PY-"+digest[:16].upper()
            db.execute("""
                INSERT OR REPLACE INTO software_resources (
                    resource_id, ecosystem, package_name, normalized_name, version, filename,
                    package_type, python_requires, platform_tag, source_url, project_url,
                    documentation_url, license, summary, requires_dist_json, sha256, bytes,
                    retrieved_utc, install_hint, compatible, metadata_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (rid,"pypi",name,normalize_package_name(name),version,path.name,ptype,"","",
                  f"pypi:{name}",f"https://pypi.org/project/{normalize_package_name(name)}/",None,"",
                  f"Cached dependency for bundle requested by {package_name}","[]",digest,path.stat().st_size,
                  utcnow(),f'python -m pip install "{path}"',int(ptype=="bdist_wheel"),
                  json.dumps({"bundle_request": package_name})))
            files.append({"resource_id":rid,"name":name,"version":version,"filename":path.name,
                          "bytes":path.stat().st_size,"sha256":digest})
        db.commit()
    finally:
        db.close()
    return {"requested":package_name,"bundle_path":str(bundle),"files":files,"count":len(files),
            "installed":False,"pip_output":proc.stdout[-2000:]}

def list_software_resources(root_path, query="", limit=500):
    root=Path(root_path).expanduser()
    db=db_connect(root)
    try:
        q=(query or "").strip().lower()
        if q:
            rows=db.execute("""
                SELECT resource_id, ecosystem, package_name, version, filename, package_type,
                       python_requires, source_url, project_url, documentation_url, license,
                       summary, requires_dist_json, sha256, bytes, retrieved_utc, install_hint, compatible
                FROM software_resources
                WHERE lower(package_name) LIKE ? OR lower(summary) LIKE ?
                ORDER BY id DESC LIMIT ?
            """, (f"%{q}%", f"%{q}%", max(1,min(int(limit),5000)))).fetchall()
        else:
            rows=db.execute("""
                SELECT resource_id, ecosystem, package_name, version, filename, package_type,
                       python_requires, source_url, project_url, documentation_url, license,
                       summary, requires_dist_json, sha256, bytes, retrieved_utc, install_hint, compatible
                FROM software_resources ORDER BY id DESC LIMIT ?
            """, (max(1,min(int(limit),5000)),)).fetchall()
    finally:
        db.close()
    keys=["resource_id","ecosystem","package_name","version","filename","package_type",
          "python_requires","source_url","project_url","documentation_url","license","summary",
          "requires_dist_json","sha256","bytes","retrieved_utc","install_hint","compatible"]
    out=[]
    for row in rows:
        d=dict(zip(keys,row)); d["requires_dist"]=json.loads(d.pop("requires_dist_json") or "[]")
        d["compatible"]=bool(d.get("compatible")); out.append(d)
    return out

def export_catalog(root, db):
    rows = db.execute("""
        SELECT object_id, sha256, filename, original_title, source_url, final_url,
               source_domain, retrieved_utc, mime_type, bytes, description, summary,
               structure_json, index_pointer_json, tags_json, source_score, search_prompt, search_query,
               volume_name, volume_member
        FROM documents ORDER BY id
    """).fetchall()
    cols = [d[0] for d in db.execute("SELECT * FROM documents LIMIT 0").description]
    # use explicit selected columns
    cols = [
        "object_id","sha256","filename","original_title","source_url","final_url",
        "source_domain","retrieved_utc","mime_type","bytes","description","summary",
        "structure_json","index_pointer_json","tags_json","source_score","search_prompt","search_query",
        "volume_name","volume_member"
    ]
    with (root / "manifest.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(cols)
        w.writerows(rows)

    checks = root / "CHECKSUMS.sha256"
    with checks.open("w", encoding="utf-8") as f:
        for oid, digest, filename in db.execute(
            "SELECT object_id, sha256, filename FROM documents ORDER BY id"
        ):
            f.write(f"{digest}  objects/{filename}\n")


def build_volumes(root, db, max_gb, log):
    """Create independent ZIPs. Each volume includes its own mini catalog."""
    volumes_dir = root / "volumes"
    volumes_dir.mkdir(exist_ok=True)
    max_bytes = int(max_gb * 1_000_000_000)  # match drive marketing units, conservative overhead below
    target = int(max_bytes * 0.96)

    docs = db.execute("""
        SELECT object_id, filename, bytes, original_title, source_url, sha256,
               description, summary, structure_json, index_pointer_json, tags_json
        FROM documents ORDER BY id
    """).fetchall()

    # Rebuild all volumes deterministically so catalog and ZIPs stay consistent.
    for old in volumes_dir.glob("HKR_*.zip"):
        old.unlink()
    db.execute("UPDATE documents SET volume_name=NULL, volume_member=NULL")
    db.commit()

    vol_num = 1
    bucket, bucket_size = [], 0

    def flush(items, number):
        if not items:
            return
        name = f"HKR_{number:03d}.zip"
        out = volumes_dir / name
        mini = []
        with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as z:
            for row in items:
                oid, filename, size, title, source, digest, desc, summary, structure, index_pointer, tags = row
                member = f"objects/{filename}"
                z.write(root / "objects" / filename, member)
                mini.append({
                    "object_id": oid, "filename": filename, "title": title,
                    "source_url": source, "sha256": digest,
                    "description": desc, "summary": summary,
                    "chapter_titles": json.loads(structure or "{}").get("chapter_titles", []),
                    "index_pointer": json.loads(index_pointer) if index_pointer else None,
                    "tags": json.loads(tags or "[]")
                })
                db.execute(
                    "UPDATE documents SET volume_name=?, volume_member=? WHERE object_id=?",
                    (name, member, oid)
                )
            z.writestr(
                "VOLUME_CATALOG.json",
                json.dumps(mini, indent=2, ensure_ascii=False)
            )
            z.writestr(
                "README.txt",
                "Independent Homestead Knowledge Repository volume.\n"
                "VOLUME_CATALOG.json describes the documents in this ZIP.\n"
            )
        db.commit()
        log(f"Built {name}: {out.stat().st_size / 1_000_000_000:.2f} GB")

    for row in docs:
        size = row[2]
        if bucket and bucket_size + size > target:
            flush(bucket, vol_num)
            vol_num += 1
            bucket, bucket_size = [], 0
        bucket.append(row)
        bucket_size += size
    flush(bucket, vol_num)

    export_catalog(root, db)
    return max(0, vol_num if docs else 0)


def collect(root_path, prompt, max_docs, per_query, log, done):
    root = Path(root_path).expanduser()
    db = db_connect(root)
    started = utcnow()
    try:
        queries = make_queries(prompt, log)
        cur = db.execute(
            "INSERT INTO research_runs(started_utc,prompt,queries_json) VALUES(?,?,?)",
            (started, prompt, json.dumps(queries, ensure_ascii=False))
        )
        run_id = cur.lastrowid
        db.commit()

        candidates = search(queries, per_query, log)
        log(f"{len(candidates)} unique candidates found.")
        saved = 0
        for item in candidates:
            if saved >= max_docs:
                break
            log(f"Checking: {(item['title'] or item['url'])[:90]}")
            fetched, reason = fetch_candidate(item, log)
            if not fetched:
                log(f"  skipped: {reason}")
                continue
            oid, reason = store_document(root, db, fetched, prompt, log)
            if oid:
                saved += 1
                log(f"  SAVED: {oid}")
            else:
                log(f"  skipped: {reason}")

        db.execute(
            "UPDATE research_runs SET downloaded_count=? WHERE id=?",
            (saved, run_id)
        )
        db.commit()
        export_catalog(root, db)
        log(f"\nCollection finished: {saved} new documents.")
        done(saved, root)
    except Exception as e:
        log(f"ERROR: {e}")
        done(0, None)
    finally:
        db.close()


def gui():
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    root = tk.Tk()
    root.title(APP_NAME)
    root.geometry("1000x740")

    settings = load_settings()
    saved_folder = settings.get("repository_folder")
    if saved_folder and not isinstance(saved_folder, str):
        saved_folder = None

    notebook = ttk.Notebook(root)
    notebook.pack(fill="both", expand=True, padx=10, pady=10)

    collect_tab = ttk.Frame(notebook, padding=12)
    batch_tab = ttk.Frame(notebook, padding=12)
    notebook.add(collect_tab, text="Research")
    notebook.add(batch_tab, text="Current Batch")

    ttk.Label(collect_tab, text="Tell the HKR Librarian what knowledge to collect:").pack(anchor="w")
    prompt_box = tk.Text(collect_tab, height=6, wrap="word")
    prompt_box.pack(fill="x", pady=(4, 10))
    prompt_box.insert("1.0", "Small diesel engine diagnosis, repair, fuel injection, cooling, lubrication, and electrical troubleshooting")

    pf = ttk.Frame(collect_tab)
    pf.pack(fill="x")
    ttk.Label(pf, text="HKR folder:").pack(side="left")
    folder_var = tk.StringVar(value=saved_folder or str(DEFAULT_ROOT))
    ttk.Entry(pf, textvariable=folder_var).pack(side="left", fill="x", expand=True, padx=6)
    ttk.Button(pf, text="Browse", command=lambda: choose_folder()).pack(side="left")

    opts = ttk.Frame(collect_tab)
    opts.pack(fill="x", pady=10)
    ttk.Label(opts, text="New docs/run:").pack(side="left")
    max_var = tk.IntVar(value=20)
    ttk.Spinbox(opts, from_=1, to=200, width=6, textvariable=max_var).pack(side="left", padx=(5,18))
    ttk.Label(opts, text="Results/query:").pack(side="left")
    depth_var = tk.IntVar(value=12)
    ttk.Spinbox(opts, from_=5, to=50, width=6, textvariable=depth_var).pack(side="left", padx=(5,18))
    ttk.Label(opts, text="ZIP max GB:").pack(side="left")
    gb_var = tk.DoubleVar(value=DEFAULT_VOLUME_GB)
    ttk.Spinbox(opts, from_=1, to=500, increment=1, width=7, textvariable=gb_var).pack(side="left", padx=5)

    logbox = tk.Text(collect_tab, height=25, state="disabled", wrap="word")
    logbox.pack(fill="both", expand=True, pady=8)

    buttons = ttk.Frame(collect_tab)
    buttons.pack(fill="x")
    collect_btn = ttk.Button(buttons, text="Research & Add to HKR")
    volume_btn = ttk.Button(buttons, text="Build Thumb-Drive ZIP Volumes")
    volume_btn.pack(side="right")
    collect_btn.pack(side="right", padx=8)

    # Current Batch page: shows uncompressed files waiting to be packed into volumes.
    summary = ttk.LabelFrame(batch_tab, text="Batch Summary", padding=10)
    summary.pack(fill="x")
    files_text = tk.StringVar(value="Files: 0")
    size_text = tk.StringVar(value="Total size: 0 B")
    remaining_text = tk.StringVar(value="Remaining to ZIP limit: --")
    ttk.Label(summary, textvariable=files_text).pack(side="left", padx=(0,25))
    ttk.Label(summary, textvariable=size_text).pack(side="left", padx=(0,25))
    ttk.Label(summary, textvariable=remaining_text).pack(side="left")

    progress = ttk.Progressbar(batch_tab, mode="determinate", maximum=100)
    progress.pack(fill="x", pady=(10, 10))

    columns = ("filename", "type", "size", "title", "source")
    tree = ttk.Treeview(batch_tab, columns=columns, show="headings")
    tree.heading("filename", text="Filename")
    tree.heading("type", text="Type")
    tree.heading("size", text="Size")
    tree.heading("title", text="Title")
    tree.heading("source", text="Source")
    tree.column("filename", width=190, anchor="w")
    tree.column("type", width=65, anchor="center")
    tree.column("size", width=90, anchor="e")
    tree.column("title", width=260, anchor="w")
    tree.column("source", width=260, anchor="w")
    yscroll = ttk.Scrollbar(batch_tab, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=yscroll.set)
    tree.pack(side="left", fill="both", expand=True, pady=(0, 45))
    yscroll.pack(side="right", fill="y", pady=(0, 45))

    batch_buttons = ttk.Frame(batch_tab)
    batch_buttons.place(relx=0, rely=1, relwidth=1, anchor="sw")
    ttk.Button(batch_buttons, text="Refresh", command=lambda: refresh_batch()).pack(side="right")

    def human_size(n):
        n = float(n or 0)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if n < 1000 or unit == "TB":
                return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
            n /= 1000

    def refresh_batch():
        for iid in tree.get_children():
            tree.delete(iid)
        repo = Path(folder_var.get()).expanduser()
        try:
            db = db_connect(repo)
            rows = db.execute("""
                SELECT filename, bytes, original_title, source_domain, source_url
                FROM documents ORDER BY id
            """).fetchall()
            db.close()
        except Exception:
            rows = []
        total = sum(r[1] or 0 for r in rows)
        limit = max(1, int(gb_var.get() * 1_000_000_000 * 0.96))
        for filename, size, title, domain, source_url in rows:
            tree.insert("", "end", values=(
                filename, Path(filename).suffix.lower().lstrip(".").upper() or "FILE",
                human_size(size), title or "", domain or source_url or ""
            ))
        files_text.set(f"Files: {len(rows):,}")
        size_text.set(f"Total size: {human_size(total)}")
        remaining = max(0, limit - total)
        remaining_text.set(f"Remaining to ZIP limit: {human_size(remaining)}")
        progress["value"] = min(100, (total / limit) * 100)

    def remember_folder():
        folder = folder_var.get().strip()
        if folder:
            settings["repository_folder"] = folder
            save_settings(settings)

    def choose_folder():
        selected = filedialog.askdirectory(initialdir=folder_var.get() or str(DEFAULT_ROOT))
        if selected:
            folder_var.set(selected)
            remember_folder()
            refresh_batch()

    def log(msg):
        def append():
            logbox.configure(state="normal")
            logbox.insert("end", msg + "\n")
            logbox.see("end")
            logbox.configure(state="disabled")
        root.after(0, append)

    def done(count, folder):
        def finish():
            collect_btn.configure(state="normal")
            refresh_batch()
            if folder:
                messagebox.showinfo(APP_NAME, f"Added {count} new documents.\n\n{folder}")
        root.after(0, finish)

    def start_collect():
        p = prompt_box.get("1.0", "end").strip()
        if not p:
            messagebox.showwarning(APP_NAME, "Enter a research request.")
            return
        remember_folder()
        collect_btn.configure(state="disabled")
        threading.Thread(target=collect, args=(folder_var.get(), p, max_var.get(), depth_var.get(), log, done), daemon=True).start()

    def start_volumes():
        remember_folder()
        def worker():
            try:
                db = db_connect(Path(folder_var.get()).expanduser())
                n = build_volumes(Path(folder_var.get()).expanduser(), db, gb_var.get(), log)
                db.close()
                root.after(0, lambda: (refresh_batch(), messagebox.showinfo(APP_NAME, f"Built {n} independent ZIP volume(s).")))
            except Exception as e:
                log(f"Volume error: {e}")
        threading.Thread(target=worker, daemon=True).start()

    collect_btn.configure(command=start_collect)
    volume_btn.configure(command=start_volumes)
    notebook.bind("<<NotebookTabChanged>>", lambda e: refresh_batch() if notebook.index(notebook.select()) == 1 else None)

    ttk.Label(collect_tab, text="The HCS internal AI plans searches and creates compact document summaries; chapter titles and index pointers are stored for navigation.").pack(anchor="w", pady=(8,0))
    def on_close():
        remember_folder()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    refresh_batch()
    root.mainloop()


if __name__ == "__main__":
    gui()


# ---------------------------------------------------------------------------
# HCS tool-service interface
# ---------------------------------------------------------------------------
def _json_or(value, fallback):
    try:
        return json.loads(value) if value else fallback
    except Exception:
        return fallback


def search_library(root_path, query, limit=8):
    """Search HKR's catalog metadata without activating files in the HCS KB."""
    root = Path(root_path).expanduser() if root_path else DEFAULT_ROOT
    db = db_connect(root)
    try:
        rows = db.execute("""
            SELECT object_id, filename, original_title, source_url, source_domain,
                   description, summary, structure_json, index_pointer_json, tags_json,
                   search_prompt, search_query, bytes
            FROM documents ORDER BY id DESC
        """).fetchall()
    finally:
        db.close()

    terms = set(re.findall(r"[A-Za-z0-9_]{2,}", (query or "").lower()))
    scored = []
    for r in rows:
        (oid, filename, title, url, domain, desc, summary, structure_json,
         index_json, tags_json, search_prompt, search_query, nbytes) = r
        chapters = _json_or(structure_json, {}).get("chapter_titles", [])
        tags = _json_or(tags_json, [])
        hay = " ".join([
            title or "", filename or "", domain or "", desc or "", summary or "",
            " ".join(map(str, chapters)), " ".join(map(str, tags)),
            search_prompt or "", search_query or ""
        ]).lower()
        ht = set(re.findall(r"[A-Za-z0-9_]{2,}", hay))
        overlap = len(terms & ht)
        phrase_bonus = 3 if query and query.lower() in hay else 0
        title_bonus = sum(2 for t in terms if t in (title or "").lower())
        score = overlap + phrase_bonus + title_bonus
        if score <= 0 and terms:
            continue
        scored.append((score, {
            "object_id": oid,
            "title": title or filename,
            "filename": filename,
            "source_url": url,
            "source_domain": domain,
            "summary": summary or desc or "",
            "chapter_titles": chapters[:40],
            "index_pointer": _json_or(index_json, None),
            "tags": tags[:30],
            "bytes": nbytes,
            "path": str((root / "objects" / filename).resolve()),
        }))
    scored.sort(key=lambda x: (x[0], x[1]["title"].lower()), reverse=True)
    return [{"score": score, **doc} for score, doc in scored[:max(1, min(int(limit), 25))]]


def relevant_sections(root_path, object_id, question, limit=6):
    """Read a selected HKR source and return the chunks most relevant to a question."""
    root = Path(root_path).expanduser() if root_path else DEFAULT_ROOT
    db = db_connect(root)
    try:
        row = db.execute(
            "SELECT filename, original_title, summary, source_url FROM documents WHERE object_id=?",
            (object_id,),
        ).fetchone()
    finally:
        db.close()
    if not row:
        raise KeyError(f"Unknown HKR object_id: {object_id}")
    filename, title, summary, source_url = row
    path = root / "objects" / filename
    if not path.exists():
        raise FileNotFoundError(path)

    suffix = path.suffix.lower()
    if suffix == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        parts=[]
        for page_no, page in enumerate(reader.pages, 1):
            txt = page.extract_text() or ""
            if txt.strip():
                parts.append((f"page {page_no}", txt))
    elif suffix in {".txt", ".md", ".csv", ".json", ".yaml", ".yml", ".toml", ".py", ".log"}:
        text=path.read_text(encoding="utf-8", errors="ignore")
        size=3000
        parts=[(f"characters {i+1}-{min(len(text), i+size)}", text[i:i+size]) for i in range(0,len(text),size)]
    else:
        raise ValueError(f"HKR section reading does not yet support {suffix or 'this file type'}")

    q=set(re.findall(r"[A-Za-z0-9_]{2,}", (question or "").lower()))
    ranked=[]
    for loc, text in parts:
        tt=set(re.findall(r"[A-Za-z0-9_]{2,}", text.lower()))
        score=len(q & tt) / max(1, len(q))
        if score > 0 or not q:
            ranked.append((score, loc, text))
    ranked.sort(key=lambda x:x[0], reverse=True)
    hits=[{"score":round(sc,3), "location":loc, "text":txt[:6000]} for sc,loc,txt in ranked[:max(1,min(int(limit),12))]]
    return {"object_id":object_id, "title":title or filename, "source_url":source_url,
            "summary":summary or "", "path":str(path.resolve()), "sections":hits}


def research_for_hcs(root_path, topic, max_docs=8, per_query=8):
    """Acquire more HKR material for HCS, then return the best matching catalog entries."""
    root = Path(root_path).expanduser() if root_path else DEFAULT_ROOT
    logs=[]
    result={"saved":0, "root":str(root)}
    def log(msg):
        logs.append(str(msg))
    def done(saved, done_root):
        result["saved"] = int(saved or 0)
        if done_root:
            result["root"] = str(done_root)
    collect(str(root), topic, max(1,min(int(max_docs),30)), max(3,min(int(per_query),20)), log, done)
    result["matches"] = search_library(str(root), topic, limit=min(10, max(4, int(max_docs))))
    result["log"] = logs[-40:]
    return result
