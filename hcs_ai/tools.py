from pathlib import Path
import os, platform, json
import psutil
from .config import load_config
from .db import audit
from . import hkr

def _allowed(path: Path) -> bool:
    cfg = load_config()
    roots = [Path(p).expanduser().resolve() for p in cfg["security"]["allowed_roots"]]
    target = path.expanduser().resolve()
    for root in roots:
        try:
            target.relative_to(root)
            return True
        except ValueError:
            pass
    return False

def system_info(args: dict):
    vm = psutil.virtual_memory()
    return {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "cpu_logical": psutil.cpu_count(logical=True),
        "memory_total_gb": round(vm.total / (1024**3), 2),
        "memory_available_gb": round(vm.available / (1024**3), 2),
        "boot_time": psutil.boot_time(),
    }

def list_processes(args: dict):
    limit = max(1, min(int(args.get("limit", 50)), 200))
    out = []
    for p in psutil.process_iter(["pid", "name", "username", "memory_info"]):
        try:
            info = p.info
            mi = info.get("memory_info")
            out.append({
                "pid": info["pid"],
                "name": info.get("name"),
                "username": info.get("username"),
                "rss_mb": round((mi.rss if mi else 0) / 1048576, 1)
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    out.sort(key=lambda x: x["rss_mb"], reverse=True)
    return out[:limit]

def list_directory(args: dict):
    path = Path(args.get("path", "."))
    if not _allowed(path):
        raise PermissionError("Path is outside configured allowed_roots")
    limit = max(1, min(int(args.get("limit", 200)), 1000))
    rows = []
    for child in list(path.expanduser().resolve().iterdir())[:limit]:
        try:
            st = child.stat()
            rows.append({"name": child.name, "path": str(child), "is_dir": child.is_dir(), "size": st.st_size})
        except OSError:
            pass
    return rows

def read_text_file(args: dict):
    path = Path(args["path"])
    if not _allowed(path):
        raise PermissionError("Path is outside configured allowed_roots")
    cfg = load_config()
    max_bytes = int(cfg["security"]["max_read_bytes"])
    resolved = path.expanduser().resolve()
    raw = resolved.read_bytes()[:max_bytes]
    return {"path": str(resolved), "text": raw.decode("utf-8", errors="replace"), "truncated": resolved.stat().st_size > len(raw)}

def environment_variables(args: dict):
    include_values = bool(args.get("include_values", False))
    if include_values:
        safe = {}
        blocked = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASS", "CREDENTIAL")
        for k, v in os.environ.items():
            safe[k] = "<redacted>" if any(x in k.upper() for x in blocked) else v
        return safe
    return sorted(os.environ.keys())


def search_hkr_library(args: dict):
    """Search the master HKR repository using a natural-language query."""
    return hkr.search_library(
        args.get("root_path") or str(hkr.DEFAULT_ROOT),
        args.get("query", ""),
        args.get("limit", 8),
    )


def read_hkr_source(args: dict):
    """Return relevant passages from one HKR document for the current question."""
    return hkr.relevant_sections(
        args.get("root_path") or str(hkr.DEFAULT_ROOT),
        args["object_id"],
        args.get("question", ""),
        args.get("limit", 6),
    )


def find_python_package(args: dict):
    """Look up current PyPI package metadata without executing package code."""
    meta = hkr.pypi_package_info(args["package_name"])
    dist = hkr.choose_python_distribution(meta, prefer_binary=True)
    return {
        "name": meta["name"], "version": meta["version"], "summary": meta["summary"],
        "requires_python": meta["requires_python"], "requires_dist": meta["requires_dist"],
        "project_url": meta["project_url"], "documentation_url": meta["documentation_url"],
        "best_distribution": {"filename": dist.get("filename"), "package_type": dist.get("packagetype"),
                              "size": dist.get("size"), "compatible": bool(dist.get("compatible"))}
    }

def cache_python_package(args: dict):
    """Download a Python package distribution into HKR without installing it."""
    return hkr.cache_python_package(
        args.get("root_path") or str(hkr.DEFAULT_ROOT), args["package_name"],
        bool(args.get("prefer_binary", True))
    )

def cache_python_bundle(args: dict):
    """Resolve and download a Python package plus dependencies into HKR without installing them."""
    return hkr.cache_python_bundle(
        args.get("root_path") or str(hkr.DEFAULT_ROOT), args["package_name"],
        bool(args.get("prefer_binary", True))
    )

def search_hkr_software(args: dict):
    return hkr.list_software_resources(
        args.get("root_path") or str(hkr.DEFAULT_ROOT), args.get("query", ""), args.get("limit", 20)
    )



def search_hkr_algorithms(args: dict):
    """Search HKR's structured algorithm/capable-knowledge database."""
    return hkr.list_algorithms(
        args.get("root_path") or str(hkr.DEFAULT_ROOT),
        args.get("query", ""), args.get("domain", ""), args.get("limit", 20)
    )

def get_hkr_algorithm(args: dict):
    return hkr.get_algorithm(args.get("root_path") or str(hkr.DEFAULT_ROOT), args["algorithm_id"])

def code_hkr_algorithm(args: dict):
    """Generate a deployment-specific implementation from a stored algorithm record."""
    return hkr.generate_algorithm_code(
        args.get("root_path") or str(hkr.DEFAULT_ROOT), args["algorithm_id"],
        args.get("language", "python"), args.get("requirements", ""), lambda _m: None
    )

def research_hkr(args: dict):
    """Search the web for additional authoritative material and add it to HKR."""
    return hkr.research_for_hcs(
        args.get("root_path") or str(hkr.DEFAULT_ROOT),
        args["topic"],
        args.get("max_docs", 8),
        args.get("per_query", 8),
    )

TOOLS = {
    "system_info": {"description": "Read basic host OS, CPU and memory information.", "function": system_info, "schema": {}},
    "list_processes": {"description": "List running processes, sorted by resident memory usage.", "function": list_processes, "schema": {"limit": "integer, optional"}},
    "list_directory": {"description": "List a directory inside configured allowed_roots.", "function": list_directory, "schema": {"path": "string", "limit": "integer, optional"}},
    "read_text_file": {"description": "Read a text file inside configured allowed_roots.", "function": read_text_file, "schema": {"path": "string"}},
    "environment_variables": {"description": "List environment variable names; optionally values with common secret variables redacted.", "function": environment_variables, "schema": {"include_values": "boolean, optional"}},
    "search_hkr_library": {"description": "Search the master HKR external library for information relevant to a natural-language query. Use this before requesting new research when active HCS knowledge is insufficient.", "function": search_hkr_library, "schema": {"query": "string", "limit": "integer, optional", "root_path": "string, optional"}},
    "read_hkr_source": {"description": "Read the most relevant sections of a specific HKR source returned by search_hkr_library. Use its object_id and the current question.", "function": read_hkr_source, "schema": {"object_id": "string", "question": "string", "limit": "integer, optional", "root_path": "string, optional"}},
    "find_python_package": {"description": "Look up a Python package on PyPI, including version, dependencies and whether a compatible wheel exists for this HCS Python runtime. This does not download or execute code.", "function": find_python_package, "schema": {"package_name": "string"}},
    "search_hkr_software": {"description": "Search Python packages and other software resources already cached in HKR.", "function": search_hkr_software, "schema": {"query": "string, optional", "limit": "integer, optional", "root_path": "string, optional"}},
    "cache_python_package": {"description": "Download and checksum a Python package distribution into the HKR software cache without installing or executing it. Use find_python_package first.", "function": cache_python_package, "schema": {"package_name": "string", "prefer_binary": "boolean, optional", "root_path": "string, optional"}},
    "cache_python_bundle": {"description": "Resolve a Python package and its dependencies with pip download, then cache/checksum all distributions in HKR without installing them. Use this for a complete offline package bundle.", "function": cache_python_bundle, "schema": {"package_name": "string", "prefer_binary": "boolean, optional", "root_path": "string, optional"}},
    "search_hkr_algorithms": {"description": "Search HKR's structured algorithm database by natural-language goal, problem type, domain, constraints, complexity, or alternatives. Use this when solving a coding/computational problem before reinventing an algorithm.", "function": search_hkr_algorithms, "schema": {"query": "string", "domain": "string, optional", "limit": "integer, optional", "root_path": "string, optional"}},
    "get_hkr_algorithm": {"description": "Retrieve the full structured HKR record for a specific algorithm, including assumptions, constraints, complexity, failure modes, pseudocode and provenance.", "function": get_hkr_algorithm, "schema": {"algorithm_id": "string", "root_path": "string, optional"}},
    "code_hkr_algorithm": {"description": "Generate a target-language implementation and tests from an HKR algorithm record, adapted to deployment requirements. Retrieve/select the algorithm first.", "function": code_hkr_algorithm, "schema": {"algorithm_id": "string", "language": "string, optional", "requirements": "string, optional", "root_path": "string, optional"}},
    "research_hkr": {"description": "Acquire additional authoritative source material into HKR when the existing HKR library does not contain enough information. Search existing HKR first. This may use the internet and can take longer than catalog search.", "function": research_hkr, "schema": {"topic": "string", "max_docs": "integer, optional", "per_query": "integer, optional", "root_path": "string, optional"}},
}

def public_tool_specs():
    return [{"name": n, "description": s["description"], "schema": s["schema"]} for n, s in TOOLS.items()]

def call_tool(name: str, args: dict):
    if name not in TOOLS:
        raise KeyError(f"Unknown tool: {name}")
    audit("tool_call", json.dumps({"name": name, "args": args}, default=str))
    return TOOLS[name]["function"](args)
