import json

import uvicorn

from .config import load_config
from .db import connect
from .ports import choose_port, save_selected_port
from .server import app


def knowledge_tree_snapshot():
    with connect() as con:
        node_rows = con.execute(
            """
            SELECT n.id,n.parent_id,n.canonical_name,n.description,n.confidence,n.review_status,
                   n.node_kind,n.created_at,n.created_by_model,
                   COUNT(DISTINCT l.artifact_id) AS direct_artifact_count
            FROM knowledge_nodes n
            LEFT JOIN knowledge_links l ON l.node_id=n.id
            GROUP BY n.id
            ORDER BY n.canonical_name COLLATE NOCASE
            """
        ).fetchall()
        alias_rows = con.execute(
            "SELECT node_id,alias FROM knowledge_node_aliases ORDER BY node_id,alias COLLATE NOCASE"
        ).fetchall()
        link_rows = con.execute(
            """
            SELECT l.node_id,l.artifact_id,l.relationship_type,l.relevance,l.confidence,l.is_primary,
                   a.artifact_type,a.title,a.storage_uri,a.content_hash,a.mime_type,a.summary,
                   a.metadata_json,a.created_at,a.updated_at
            FROM knowledge_links l
            JOIN knowledge_artifacts a ON a.id=l.artifact_id
            ORDER BY l.node_id,a.title COLLATE NOCASE
            """
        ).fetchall()
        relation_rows = con.execute(
            """
            SELECT r.source_node_id,r.target_node_id,r.relationship_type,r.confidence,
                   s.canonical_name AS source_name,t.canonical_name AS target_name
            FROM knowledge_relationships r
            JOIN knowledge_nodes s ON s.id=r.source_node_id
            JOIN knowledge_nodes t ON t.id=r.target_node_id
            ORDER BY s.canonical_name COLLATE NOCASE,t.canonical_name COLLATE NOCASE
            """
        ).fetchall()

    aliases = {}
    for row in alias_rows:
        aliases.setdefault(int(row["node_id"]), []).append(row["alias"])

    nodes = []
    for row in node_rows:
        item = dict(row)
        item["aliases"] = aliases.get(int(row["id"]), [])
        nodes.append(item)

    links = []
    artifact_ids = set()
    for row in link_rows:
        item = dict(row)
        item["is_primary"] = bool(item["is_primary"])
        try:
            item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
        except json.JSONDecodeError:
            item["metadata"] = {}
            item.pop("metadata_json", None)
        artifact_ids.add(int(item["artifact_id"]))
        links.append(item)

    return {
        "nodes": nodes,
        "links": links,
        "relationships": [dict(r) for r in relation_rows],
        "counts": {
            "nodes": len(nodes),
            "artifacts": len(artifact_ids),
            "links": len(links),
            "relationships": len(relation_rows),
        },
    }


@app.get("/knowledge/tree")
def knowledge_tree():
    return knowledge_tree_snapshot()


def main():
    cfg = load_config()["server"]
    host = cfg["host"]
    port = choose_port(host)
    save_selected_port(host, port)
    if port != int(cfg["port"]):
        print(f"Port {cfg['port']} is busy; HCS-AI is using {port} instead.")
    uvicorn.run("hcs_ai.server_tree:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
