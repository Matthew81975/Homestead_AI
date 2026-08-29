import json
import urllib.error
import urllib.request
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
import uvicorn

from .config import load_config, save_config
from .db import init_db, connect, now_iso, audit
from .llm import chat as llm_chat
from .knowledge import import_path, search as kb_search, remove_source, active_sources
from .tools import public_tool_specs, call_tool
from . import hkr
from .ports import choose_port, save_selected_port
from . import engine
from . import cloud_router

app = FastAPI(title="HCS-AI", version="0.7.1")

class ChatIn(BaseModel):
    message: str
    history: list[dict] = Field(default_factory=list)
    use_kb: bool = True
    task_id: str = "alexandria-default"

class MemoryIn(BaseModel):
    key: str
    value: str

class KnowledgeImportIn(BaseModel):
    path: str

class ToolCallIn(BaseModel):
    name: str
    args: dict = Field(default_factory=dict)


class ExternalFilesIn(BaseModel):
    root_path: str
    object_ids: list[str] = Field(default_factory=list)

class HKRCollectIn(BaseModel):
    root_path: str
    prompt: str
    max_docs: int = 20
    per_query: int = 12

class HKRVolumesIn(BaseModel):
    root_path: str
    max_gb: float = 28.0

class HKRPythonPackageIn(BaseModel):
    root_path: str
    package_name: str
    prefer_binary: bool = True


class HKRAlgorithmScanIn(BaseModel):
    root_path: str
    object_ids: list[str] = Field(default_factory=list)
    limit: int = 500

class HKRAlgorithmCodeIn(BaseModel):
    root_path: str
    algorithm_id: str
    language: str = "python"
    requirements: str = ""

class MCPServerIn(BaseModel):
    name: str
    transport: str = "stdio"
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    url: str | None = None
    enabled: bool = True

class InferenceConfigIn(BaseModel):
    model_path: str
    auto_start: bool = True

class AIModeIn(BaseModel):
    mode: str


class TierApprovalIn(BaseModel):
    task_id: str
    tier: str

@app.on_event("startup")
def startup():
    init_db()
    engine.auto_start()
    audit("startup", "HCS-AI server started")

@app.on_event("shutdown")
def shutdown():
    engine.stop()

@app.get("/health")
def health():
    cfg = load_config()
    return {"ok": True, "name": cfg["app"]["name"], "version": cfg["app"]["version"]}

@app.get("/inference/status")
def inference_status():
    return engine.status()

@app.post("/inference/start")
def inference_start():
    try:
        return engine.start()
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/inference/stop")
def inference_stop():
    return engine.stop()

@app.post("/inference/config")
def inference_config(inp: InferenceConfigIn):
    try:
        engine.stop()
        engine.configure(inp.model_path, inp.auto_start)
        return engine.start() if inp.auto_start else engine.status()
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


def _ai_mode_status():
    cfg = load_config()
    selected = str(cfg.get("ai", {}).get("mode") or "offline").lower()
    cloud = cfg.get("cloud_ai", {})
    routes = [
        route for route in cloud.get("routes", [])
        if route.get("enabled", True)
    ]
    probe_base = (
        str(routes[0].get("base_url") or "").rstrip("/")
        if routes else "https://api.openai.com/v1"
    )
    url = probe_base + "/models"
    timeout = float(cfg.get("ai", {}).get("connectivity_timeout_seconds", 2.0))
    internet = False
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "HCS-AI/0.10"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=timeout):
            internet = True
    except urllib.error.HTTPError:
        internet = True
    except Exception:
        internet = False

    pool = cloud_router.cloud_status()
    cloud_configured = bool(
        pool.get("enabled") and pool.get("configured_routes", 0) > 0
    )
    live_available = bool(
        internet and cloud_configured and pool.get("healthy_routes", 0) > 0
    )
    effective = "live" if selected == "live" and live_available else "offline"
    return {
        "selected_mode": selected,
        "effective_mode": effective,
        "internet_available": internet,
        "cloud_configured": cloud_configured,
        "live_available": live_available,
        "cloud_provider": "",
        "cloud_model": "",
        "cloud_pool": pool,
    }

@app.get("/ai/status")
def ai_status():
    return _ai_mode_status()

@app.post("/ai/mode")
def ai_set_mode(inp: AIModeIn):
    mode = str(inp.mode or "").strip().lower()
    if mode not in ("offline", "live"):
        raise HTTPException(status_code=400, detail="AI mode must be Offline or Live.")
    status = _ai_mode_status()
    if mode == "live" and not status["live_available"]:
        if not status["internet_available"]:
            raise HTTPException(status_code=400, detail="Live mode is unavailable: no Internet connection.")
        raise HTTPException(status_code=400, detail="Internet is available, but cloud AI is not configured yet.")
    cfg = load_config()
    cfg.setdefault("ai", {})["mode"] = mode
    save_config(cfg)
    audit("ai_mode", mode)
    return _ai_mode_status()

@app.get("/ai/models")
def ai_models(task_id: str | None = None):
    return cloud_router.model_inventory(task_id)


@app.post("/ai/approve-tier")
def ai_approve_tier(inp: TierApprovalIn):
    return cloud_router.approve_tier_change(inp.task_id, inp.tier)


@app.post("/chat")
def chat(inp: ChatIn):
    try:
        audit("chat", inp.message[:1000])
        status = _ai_mode_status()
        if status["effective_mode"] != "live":
            return llm_chat(inp.message, inp.history, inp.use_kb)

        cfg = load_config()
        messages = [{"role": "system", "content": cfg["app"]["system_prompt"]}]
        messages.extend(inp.history[-12:])
        messages.append({"role": "user", "content": inp.message})
        result = cloud_router.chat(inp.task_id, messages)
        for event in result.get("route_events", []):
            audit(
                "cloud_route_event",
                json.dumps({
                    "task_id": inp.task_id,
                    "route": event.get("route"),
                    "provider": event.get("provider"),
                    "model": event.get("model"),
                    "tier": event.get("tier"),
                    "outcome": event.get("outcome"),
                    "reason": event.get("reason"),
                }),
            )
        audit(
            "cloud_route",
            json.dumps({
                "task_id": inp.task_id,
                "provider": result.get("provider"),
                "model": result.get("model"),
                "tier": result.get("tier") or result.get("current_tier"),
                "approval_required": bool(result.get("approval_required")),
            }),
        )
        return result
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"LLM request failed: {e}")


@app.get("/memory")
def memory_list(limit: int = 100):
    with connect() as con:
        rows = con.execute("SELECT id,created_at,key,value FROM memory ORDER BY id DESC LIMIT ?", (max(1, min(limit, 1000)),)).fetchall()
    return [dict(r) for r in rows]

@app.post("/memory")
def memory_add(inp: MemoryIn):
    with connect() as con:
        cur = con.execute("INSERT INTO memory(created_at,key,value) VALUES(?,?,?)", (now_iso(), inp.key, inp.value))
    audit("memory_add", inp.key)
    return {"ok": True, "id": cur.lastrowid}

@app.post("/knowledge/import")
def knowledge_import(inp: KnowledgeImportIn):
    try:
        result = import_path(inp.path)
        audit("knowledge_import", json.dumps({"path": inp.path, **result}))
        return result
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/knowledge/search")
def knowledge_search(q: str, limit: int = 6):
    return kb_search(q, limit=max(1, min(limit, 20)))


@app.get("/hkr/status")
def hkr_status(root_path: str | None = None):
    root = hkr.Path(root_path).expanduser() if root_path else hkr.DEFAULT_ROOT
    db = hkr.db_connect(root)
    try:
        count, total = db.execute("SELECT COUNT(*), COALESCE(SUM(bytes),0) FROM documents").fetchone()
    finally:
        db.close()
    return {"root": str(root), "documents": count, "bytes": total}

@app.get("/hkr/documents")
def hkr_documents(root_path: str | None = None, limit: int = 200):
    root = hkr.Path(root_path).expanduser() if root_path else hkr.DEFAULT_ROOT
    db = hkr.db_connect(root)
    try:
        rows = db.execute("""
            SELECT object_id, original_title, filename, bytes, source_domain, source_url,
                   description, summary, structure_json, index_pointer_json, tags_json
            FROM documents ORDER BY id DESC LIMIT ?
        """, (max(1, min(limit, 2000)),)).fetchall()
    finally:
        db.close()
    out=[]
    for r in rows:
        d=dict(zip(["object_id","original_title","filename","bytes","source_domain","source_url","description","summary","structure_json","index_pointer_json","tags_json"], r))
        d["chapter_titles"] = json.loads(d.pop("structure_json") or "{}").get("chapter_titles", [])
        d["index_pointer"] = json.loads(d.pop("index_pointer_json")) if d.get("index_pointer_json") else None
        d.pop("index_pointer_json", None)
        d["tags"] = json.loads(d.pop("tags_json") or "[]")
        out.append(d)
    return out


@app.get("/external/files")
def external_files(root_path: str | None = None, limit: int = 5000):
    root = hkr.Path(root_path).expanduser() if root_path else hkr.DEFAULT_ROOT
    db = hkr.db_connect(root)
    try:
        rows = db.execute("""
            SELECT object_id, original_title, filename, bytes, source_domain, source_url,
                   description, summary, structure_json, index_pointer_json, tags_json
            FROM documents ORDER BY original_title COLLATE NOCASE, id
            LIMIT ?
        """, (max(1, min(limit, 20000)),)).fetchall()
    finally:
        db.close()
    active = active_sources()
    out = []
    for r in rows:
        d = dict(zip(["object_id","original_title","filename","bytes","source_domain","source_url",
                      "description","summary","structure_json","index_pointer_json","tags_json"], r))
        source_path = str((root / "objects" / d["filename"]).resolve())
        d["path"] = source_path
        d["active"] = source_path in active
        d["chapter_titles"] = json.loads(d.pop("structure_json") or "{}").get("chapter_titles", [])
        d["index_pointer"] = json.loads(d.pop("index_pointer_json")) if d.get("index_pointer_json") else None
        d.pop("index_pointer_json", None)
        d["tags"] = json.loads(d.pop("tags_json") or "[]")
        out.append(d)
    return out

@app.post("/external/add")
def external_add(inp: ExternalFilesIn):
    root = hkr.Path(inp.root_path).expanduser()
    db = hkr.db_connect(root)
    added_files = 0
    added_chunks = 0
    missing = []
    try:
        for oid in inp.object_ids:
            row = db.execute("SELECT filename FROM documents WHERE object_id=?", (oid,)).fetchone()
            if not row:
                missing.append(oid); continue
            path = root / "objects" / row[0]
            if not path.exists():
                missing.append(oid); continue
            result = import_path(str(path))
            added_files += result.get("files_imported", 0)
            added_chunks += result.get("chunks", 0)
    finally:
        db.close()
    audit("external_add", json.dumps({"root": str(root), "object_ids": inp.object_ids, "files": added_files}))
    return {"ok": True, "files_added": added_files, "chunks": added_chunks, "missing": missing}

@app.post("/external/remove")
def external_remove(inp: ExternalFilesIn):
    root = hkr.Path(inp.root_path).expanduser()
    db = hkr.db_connect(root)
    removed_files = 0
    removed_chunks = 0
    missing = []
    try:
        for oid in inp.object_ids:
            row = db.execute("SELECT filename FROM documents WHERE object_id=?", (oid,)).fetchone()
            if not row:
                missing.append(oid); continue
            path = root / "objects" / row[0]
            result = remove_source(str(path))
            removed_files += 1
            removed_chunks += max(0, result.get("chunks_removed", 0))
    finally:
        db.close()
    audit("external_remove", json.dumps({"root": str(root), "object_ids": inp.object_ids, "files": removed_files}))
    return {"ok": True, "files_removed": removed_files, "chunks_removed": removed_chunks, "missing": missing}

@app.post("/hkr/collect")
def hkr_collect(inp: HKRCollectIn):
    logs=[]
    result={"saved": 0, "root": None}
    def log(msg): logs.append(str(msg))
    def done(saved, root):
        result["saved"] = saved
        result["root"] = str(root) if root else None
    hkr.collect(inp.root_path, inp.prompt, max(1,min(inp.max_docs,200)), max(5,min(inp.per_query,50)), log, done)
    # HKR owns the master library. Files become active HCS knowledge only when
    # selected in the External Library tab.
    audit("hkr_collect", json.dumps({"prompt": inp.prompt[:500], **result}))
    return {**result, "log": logs}

@app.post("/hkr/volumes")
def hkr_volumes(inp: HKRVolumesIn):
    root = hkr.Path(inp.root_path).expanduser()
    db = hkr.db_connect(root)
    logs=[]
    try:
        count = hkr.build_volumes(root, db, max(1.0, inp.max_gb), logs.append)
    finally:
        db.close()
    return {"volumes": count, "log": logs}


@app.get("/hkr/software/python/info")
def hkr_python_info(package_name: str):
    try:
        meta = hkr.pypi_package_info(package_name)
        dist = hkr.choose_python_distribution(meta, prefer_binary=True)
        return {
            "name": meta["name"], "version": meta["version"], "summary": meta["summary"],
            "license": meta["license"], "requires_python": meta["requires_python"],
            "requires_dist": meta["requires_dist"], "project_url": meta["project_url"],
            "documentation_url": meta["documentation_url"],
            "best_distribution": {
                "filename": dist.get("filename"), "package_type": dist.get("packagetype"),
                "size": dist.get("size"), "compatible": bool(dist.get("compatible"))
            }
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/hkr/software/python/cache")
def hkr_python_cache(inp: HKRPythonPackageIn):
    try:
        result = hkr.cache_python_package(inp.root_path, inp.package_name, inp.prefer_binary)
        audit("hkr_python_cache", json.dumps({"package": inp.package_name, "resource_id": result["resource_id"]}))
        return result
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/hkr/software/python/cache-bundle")
def hkr_python_cache_bundle(inp: HKRPythonPackageIn):
    try:
        result = hkr.cache_python_bundle(inp.root_path, inp.package_name, inp.prefer_binary)
        audit("hkr_python_cache_bundle", json.dumps({"package": inp.package_name, "count": result["count"]}))
        return result
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/hkr/software")
def hkr_software(root_path: str | None = None, q: str = "", limit: int = 500):
    root = str(hkr.Path(root_path).expanduser() if root_path else hkr.DEFAULT_ROOT)
    return hkr.list_software_resources(root, q, limit)


@app.get("/hkr/algorithms")
def hkr_algorithms(root_path: str | None = None, q: str = "", domain: str = "", limit: int = 1000):
    root = str(hkr.Path(root_path).expanduser() if root_path else hkr.DEFAULT_ROOT)
    return hkr.list_algorithms(root, q, domain, limit)

@app.get("/hkr/algorithms/{algorithm_id}")
def hkr_algorithm_detail(algorithm_id: str, root_path: str | None = None):
    root = str(hkr.Path(root_path).expanduser() if root_path else hkr.DEFAULT_ROOT)
    out = hkr.get_algorithm(root, algorithm_id)
    if not out:
        raise HTTPException(status_code=404, detail="Algorithm not found")
    return out

@app.post("/hkr/algorithms/scan")
def hkr_algorithm_scan(inp: HKRAlgorithmScanIn):
    logs=[]
    try:
        out=hkr.scan_algorithms(inp.root_path, inp.object_ids or None, inp.limit, logs.append)
        audit("hkr_algorithm_scan", json.dumps(out))
        return {**out, "log":logs}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/hkr/algorithms/code")
def hkr_algorithm_code(inp: HKRAlgorithmCodeIn):
    logs=[]
    try:
        out=hkr.generate_algorithm_code(inp.root_path, inp.algorithm_id, inp.language, inp.requirements, logs.append)
        audit("hkr_algorithm_code", json.dumps({"algorithm_id":inp.algorithm_id,"language":inp.language}))
        return {**out, "log":logs}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/tools")
def tools():
    return public_tool_specs()

@app.post("/tools/call")
def tool_call(inp: ToolCallIn):
    try:
        return {"ok": True, "result": call_tool(inp.name, inp.args)}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/mcp/servers")
def mcp_list():
    with connect() as con:
        rows = con.execute("SELECT id,name,transport,command,args_json,url,enabled FROM mcp_servers ORDER BY name").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["args"] = json.loads(d.pop("args_json") or "[]")
        d["enabled"] = bool(d["enabled"])
        out.append(d)
    return out

@app.post("/mcp/servers")
def mcp_add(inp: MCPServerIn):
    with connect() as con:
        con.execute("""
            INSERT INTO mcp_servers(name,transport,command,args_json,url,enabled)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(name) DO UPDATE SET
              transport=excluded.transport,
              command=excluded.command,
              args_json=excluded.args_json,
              url=excluded.url,
              enabled=excluded.enabled
        """, (inp.name, inp.transport, inp.command, json.dumps(inp.args), inp.url, int(inp.enabled)))
    audit("mcp_registry_update", inp.name)
    return {"ok": True}

def main():
    cfg = load_config()["server"]
    host = cfg["host"]
    port = choose_port(host)
    save_selected_port(host, port)
    if port != int(cfg["port"]):
        print(f"Port {cfg['port']} is busy; HCS-AI is using {port} instead.")
    uvicorn.run("hcs_ai.server:app", host=host, port=port, reload=False)

if __name__ == "__main__":
    main()
