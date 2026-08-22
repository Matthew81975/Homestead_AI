from pathlib import Path
import sqlite3
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "hcs_ai.sqlite3"

def connect():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con

def init_db():
    with connect() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS kb_chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            chunk_index INTEGER NOT NULL,
            text TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS mcp_servers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            transport TEXT NOT NULL,
            command TEXT,
            args_json TEXT,
            url TEXT,
            enabled INTEGER NOT NULL DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            action TEXT NOT NULL,
            detail TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS knowledge_nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            parent_id INTEGER REFERENCES knowledge_nodes(id) ON DELETE CASCADE,
            canonical_name TEXT NOT NULL,
            normalized_name TEXT NOT NULL,
            description TEXT,
            node_kind TEXT NOT NULL DEFAULT 'subject',
            created_at TEXT NOT NULL,
            created_by_artifact_id INTEGER,
            created_by_model TEXT,
            confidence REAL NOT NULL DEFAULT 1.0,
            creation_reason TEXT,
            review_status TEXT NOT NULL DEFAULT 'accepted',
            UNIQUE(parent_id, normalized_name)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_knowledge_root_name
            ON knowledge_nodes(normalized_name) WHERE parent_id IS NULL;
        CREATE INDEX IF NOT EXISTS idx_knowledge_nodes_parent ON knowledge_nodes(parent_id);

        CREATE TABLE IF NOT EXISTS knowledge_node_aliases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            node_id INTEGER NOT NULL REFERENCES knowledge_nodes(id) ON DELETE CASCADE,
            alias TEXT NOT NULL,
            normalized_alias TEXT NOT NULL,
            UNIQUE(node_id, normalized_alias)
        );
        CREATE INDEX IF NOT EXISTS idx_knowledge_alias_lookup ON knowledge_node_aliases(normalized_alias);

        CREATE TABLE IF NOT EXISTS knowledge_artifacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            artifact_type TEXT NOT NULL,
            title TEXT NOT NULL,
            storage_uri TEXT,
            content_hash TEXT,
            mime_type TEXT,
            summary TEXT,
            metadata_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_knowledge_artifact_uri
            ON knowledge_artifacts(storage_uri) WHERE storage_uri IS NOT NULL;
        CREATE UNIQUE INDEX IF NOT EXISTS idx_knowledge_artifact_hash
            ON knowledge_artifacts(content_hash) WHERE content_hash IS NOT NULL;

        CREATE TABLE IF NOT EXISTS knowledge_links (
            node_id INTEGER NOT NULL REFERENCES knowledge_nodes(id) ON DELETE CASCADE,
            artifact_id INTEGER NOT NULL REFERENCES knowledge_artifacts(id) ON DELETE CASCADE,
            relationship_type TEXT NOT NULL DEFAULT 'about',
            relevance REAL NOT NULL DEFAULT 1.0,
            confidence REAL NOT NULL DEFAULT 1.0,
            is_primary INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            PRIMARY KEY (node_id, artifact_id, relationship_type)
        );
        CREATE INDEX IF NOT EXISTS idx_knowledge_links_artifact ON knowledge_links(artifact_id);

        CREATE TABLE IF NOT EXISTS knowledge_classifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            artifact_id INTEGER NOT NULL REFERENCES knowledge_artifacts(id) ON DELETE CASCADE,
            model_name TEXT,
            analysis_json TEXT NOT NULL,
            confidence REAL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS knowledge_node_provenance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            node_id INTEGER NOT NULL REFERENCES knowledge_nodes(id) ON DELETE CASCADE,
            artifact_id INTEGER REFERENCES knowledge_artifacts(id) ON DELETE SET NULL,
            model_name TEXT,
            confidence REAL,
            reason TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS knowledge_relationships (
            source_node_id INTEGER NOT NULL REFERENCES knowledge_nodes(id) ON DELETE CASCADE,
            target_node_id INTEGER NOT NULL REFERENCES knowledge_nodes(id) ON DELETE CASCADE,
            relationship_type TEXT NOT NULL,
            confidence REAL NOT NULL DEFAULT 1.0,
            created_at TEXT NOT NULL,
            PRIMARY KEY(source_node_id, target_node_id, relationship_type)
        );
        """)

def now_iso():
    return datetime.now(timezone.utc).isoformat()

def audit(action: str, detail: str):
    with connect() as con:
        con.execute(
            "INSERT INTO audit_log(created_at, action, detail) VALUES(?,?,?)",
            (now_iso(), action, detail),
        )
