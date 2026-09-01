import hashlib
import json
import re
from pathlib import Path

import httpx

from .config import llm_config, load_config
from .db import connect, now_iso
from . import cloud_router

AUTO_CREATE_THRESHOLD = 0.78
REVIEW_THRESHOLD = 0.55


def normalize_name(value: str) -> str:
    value = (value or "").strip().lower()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _artifact_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def register_artifact(
    *, artifact_type: str, title: str, storage_uri: str | None = None,
    content_hash: str | None = None, mime_type: str | None = None,
    summary: str | None = None, metadata: dict | None = None,
) -> int:
    now = now_iso()
    with connect() as con:
        if storage_uri:
            row = con.execute("SELECT id FROM knowledge_artifacts WHERE storage_uri=?", (storage_uri,)).fetchone()
            if row:
                artifact_id = int(row["id"])
                con.execute(
                    """UPDATE knowledge_artifacts SET artifact_type=?, title=?, content_hash=COALESCE(?,content_hash),
                       mime_type=COALESCE(?,mime_type), summary=COALESCE(?,summary), metadata_json=?, updated_at=? WHERE id=?""",
                    (artifact_type, title, content_hash, mime_type, summary, json.dumps(metadata or {}), now, artifact_id),
                )
                return artifact_id
        if content_hash:
            row = con.execute("SELECT id FROM knowledge_artifacts WHERE content_hash=?", (content_hash,)).fetchone()
            if row:
                return int(row["id"])
        cur = con.execute(
            """INSERT INTO knowledge_artifacts
               (artifact_type,title,storage_uri,content_hash,mime_type,summary,metadata_json,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (artifact_type, title, storage_uri, content_hash, mime_type, summary, json.dumps(metadata or {}), now, now),
        )
        return int(cur.lastrowid)


def register_file_artifact(path: str | Path, artifact_type: str | None = None, mime_type: str | None = None) -> int:
    p = Path(path).expanduser().resolve()
    return register_artifact(
        artifact_type=artifact_type or (p.suffix.lower().lstrip(".") or "file"),
        title=p.name,
        storage_uri=str(p),
        content_hash=_artifact_hash(p),
        mime_type=mime_type,
        metadata={"filename": p.name, "extension": p.suffix.lower()},
    )


def _existing_tree_snapshot() -> list[dict]:
    with connect() as con:
        rows = con.execute(
            "SELECT id,parent_id,canonical_name,normalized_name,description,review_status FROM knowledge_nodes ORDER BY id"
        ).fetchall()
        aliases = con.execute("SELECT node_id,alias FROM knowledge_node_aliases ORDER BY node_id,id").fetchall()
    alias_map: dict[int, list[str]] = {}
    for row in aliases:
        alias_map.setdefault(int(row["node_id"]), []).append(row["alias"])
    return [{**dict(row), "aliases": alias_map.get(int(row["id"]), [])} for row in rows]


def _resolve_model(client: httpx.Client, cfg: dict) -> str:
    configured = cfg.get("model", "auto")
    if configured and configured != "auto":
        return configured
    r = client.get(cfg["base_url"].rstrip("/") + "/models")
    r.raise_for_status()
    models = r.json().get("data", [])
    if not models:
        raise RuntimeError("No model is loaded in the HCS local inference engine.")
    return models[0]["id"]


def analyze_for_tree(*, title: str, artifact_type: str, text: str, metadata: dict | None = None) -> dict:
    tree = _existing_tree_snapshot()
    prompt = f"""You are the taxonomy classifier for HCS. Analyze the supplied knowledge artifact and place it in a hierarchical knowledge taxonomy.

Rules:
- Classify by subject/domain, NEVER by file type.
- Paths may be arbitrarily deep.
- Reuse existing canonical nodes/aliases when semantically equivalent.
- Propose a new node only when a genuinely distinct field/subfield is needed.
- Prefer established academic/technical field names.
- An artifact may belong to multiple paths.
- Give one primary path and zero or more additional paths.
- Confidence is 0.0 to 1.0.
- Do not create trivial nodes from incidental words.
- Return JSON only.

Schema:
{{
  "summary": "short semantic summary",
  "primary_path": [{{"name":"Field","confidence":0.95,"reason":"...","aliases":[]}}],
  "additional_paths": [[{{"name":"Field","confidence":0.9,"reason":"...","aliases":[]}}]],
  "relationships": [{{"source_path":["A","B"],"type":"uses|related_to|part_of|applies_to","target_path":["C","D"],"confidence":0.8}}],
  "overall_confidence": 0.9
}}

Existing tree:
{json.dumps(tree, ensure_ascii=False)[:30000]}

Artifact type: {artifact_type}
Title: {title}
Metadata: {json.dumps(metadata or {}, ensure_ascii=False)}

Artifact content:
{text[:70000]}
"""
    mode = str(load_config().get("ai", {}).get("mode") or "offline").lower()
    if mode == "live":
        result = cloud_router.chat(
            "knowledge-tree-classifier",
            [{"role": "user", "content": prompt}],
        )
        if result.get("approval_required"):
            raise RuntimeError(
                "Knowledge Tree classification requires approval before changing AI capability tier."
            )
        raw = str(result.get("text") or "").strip()
        provider = str(result.get("provider") or "cloud")
        model = str(result.get("model") or "unknown")
        model_name = f"{provider}/{model}"
    else:
        cfg = llm_config()
        url = cfg["base_url"].rstrip("/") + "/chat/completions"
        with httpx.Client(timeout=max(120, int(cfg.get("timeout_seconds", 120)))) as client:
            model = _resolve_model(client, cfg)
            r = client.post(url, json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
            })
            r.raise_for_status()
            raw = (r.json()["choices"][0]["message"].get("content") or "").strip()
        model_name = model
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end < start:
        raise RuntimeError("Knowledge classifier did not return JSON.")
    result = json.loads(raw[start:end + 1])
    result["model_name"] = model_name
    return result


def _find_child(con, parent_id: int | None, name: str):
    normalized = normalize_name(name)
    if parent_id is None:
        row = con.execute(
            "SELECT * FROM knowledge_nodes WHERE parent_id IS NULL AND normalized_name=?", (normalized,)
        ).fetchone()
    else:
        row = con.execute(
            "SELECT * FROM knowledge_nodes WHERE parent_id=? AND normalized_name=?", (parent_id, normalized)
        ).fetchone()
    if row:
        return row
    alias_rows = con.execute(
        """SELECT n.* FROM knowledge_nodes n JOIN knowledge_node_aliases a ON a.node_id=n.id
           WHERE a.normalized_alias=? AND ((? IS NULL AND n.parent_id IS NULL) OR n.parent_id=?) LIMIT 1""",
        (normalized, parent_id, parent_id),
    ).fetchone()
    return alias_rows


def _ensure_path(con, artifact_id: int, path_spec: list[dict], model_name: str) -> tuple[int | None, float]:
    parent_id = None
    path_conf = 1.0
    for part in path_spec:
        if isinstance(part, str):
            part = {"name": part, "confidence": 0.8, "reason": "LLM classification", "aliases": []}
        name = str(part.get("name") or "").strip()
        if not name:
            continue
        confidence = float(part.get("confidence", 0.75))
        path_conf = min(path_conf, confidence)
        row = _find_child(con, parent_id, name)
        if row:
            node_id = int(row["id"])
        else:
            review_status = "accepted" if confidence >= AUTO_CREATE_THRESHOLD else "pending_review"
            if confidence < REVIEW_THRESHOLD:
                # Very weak guesses are not allowed to pollute the tree at all.
                return None, path_conf
            cur = con.execute(
                """INSERT INTO knowledge_nodes
                   (parent_id,canonical_name,normalized_name,description,created_at,created_by_artifact_id,
                    created_by_model,confidence,creation_reason,review_status)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (parent_id, name, normalize_name(name), part.get("description"), now_iso(), artifact_id,
                 model_name, confidence, part.get("reason"), review_status),
            )
            node_id = int(cur.lastrowid)
            con.execute(
                """INSERT INTO knowledge_node_provenance(node_id,artifact_id,model_name,confidence,reason,created_at)
                   VALUES(?,?,?,?,?,?)""",
                (node_id, artifact_id, model_name, confidence, part.get("reason"), now_iso()),
            )
            for alias in part.get("aliases") or []:
                alias = str(alias).strip()
                if alias:
                    con.execute(
                        "INSERT OR IGNORE INTO knowledge_node_aliases(node_id,alias,normalized_alias) VALUES(?,?,?)",
                        (node_id, alias, normalize_name(alias)),
                    )
        parent_id = node_id
    return parent_id, path_conf


def apply_classification(artifact_id: int, analysis: dict) -> dict:
    model_name = analysis.get("model_name") or "unknown"
    paths = []
    primary = analysis.get("primary_path") or []
    if primary:
        paths.append((primary, True))
    for path in analysis.get("additional_paths") or []:
        if path:
            paths.append((path, False))

    linked = []
    pending = []
    with connect() as con:
        con.execute(
            "INSERT INTO knowledge_classifications(artifact_id,model_name,analysis_json,confidence,created_at) VALUES(?,?,?,?,?)",
            (artifact_id, model_name, json.dumps(analysis), analysis.get("overall_confidence"), now_iso()),
        )
        if analysis.get("summary"):
            con.execute("UPDATE knowledge_artifacts SET summary=?,updated_at=? WHERE id=?", (analysis["summary"], now_iso(), artifact_id))
        for spec, is_primary in paths:
            node_id, confidence = _ensure_path(con, artifact_id, spec, model_name)
            if not node_id:
                continue
            node = con.execute("SELECT canonical_name,review_status FROM knowledge_nodes WHERE id=?", (node_id,)).fetchone()
            con.execute(
                """INSERT INTO knowledge_links(node_id,artifact_id,relationship_type,relevance,confidence,is_primary,created_at)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(node_id,artifact_id,relationship_type) DO UPDATE SET
                   relevance=excluded.relevance,confidence=excluded.confidence,is_primary=MAX(is_primary,excluded.is_primary)""",
                (node_id, artifact_id, "about", 1.0, confidence, 1 if is_primary else 0, now_iso()),
            )
            linked.append({"node_id": node_id, "name": node["canonical_name"], "primary": is_primary, "confidence": confidence})
            if node["review_status"] == "pending_review":
                pending.append(node_id)

        for rel in analysis.get("relationships") or []:
            source_spec = [{"name": n, "confidence": rel.get("confidence", 0.7), "reason": "LLM relationship"} for n in rel.get("source_path") or []]
            target_spec = [{"name": n, "confidence": rel.get("confidence", 0.7), "reason": "LLM relationship"} for n in rel.get("target_path") or []]
            source_id, _ = _ensure_path(con, artifact_id, source_spec, model_name)
            target_id, _ = _ensure_path(con, artifact_id, target_spec, model_name)
            if source_id and target_id and source_id != target_id:
                con.execute(
                    """INSERT OR REPLACE INTO knowledge_relationships
                       (source_node_id,target_node_id,relationship_type,confidence,created_at) VALUES(?,?,?,?,?)""",
                    (source_id, target_id, str(rel.get("type") or "related_to"), float(rel.get("confidence", 0.7)), now_iso()),
                )
    return {"artifact_id": artifact_id, "linked": linked, "pending_review_node_ids": sorted(set(pending))}


def classify_artifact(*, artifact_id: int, title: str, artifact_type: str, text: str, metadata: dict | None = None) -> dict:
    analysis = analyze_for_tree(title=title, artifact_type=artifact_type, text=text, metadata=metadata)
    applied = apply_classification(artifact_id, analysis)
    return {"analysis": analysis, **applied}


def tree_rows() -> list[dict]:
    with connect() as con:
        rows = con.execute(
            """SELECT n.id,n.parent_id,n.canonical_name,n.description,n.confidence,n.review_status,
                      COUNT(DISTINCT l.artifact_id) AS artifact_count
               FROM knowledge_nodes n LEFT JOIN knowledge_links l ON l.node_id=n.id
               GROUP BY n.id ORDER BY n.canonical_name COLLATE NOCASE"""
        ).fetchall()
    return [dict(r) for r in rows]


def artifact_links(artifact_id: int) -> list[dict]:
    with connect() as con:
        rows = con.execute(
            """SELECT n.id,n.parent_id,n.canonical_name,l.relationship_type,l.relevance,l.confidence,l.is_primary
               FROM knowledge_links l JOIN knowledge_nodes n ON n.id=l.node_id WHERE l.artifact_id=?""",
            (artifact_id,),
        ).fetchall()
    return [dict(r) for r in rows]
