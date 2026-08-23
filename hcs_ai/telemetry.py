from __future__ import annotations

import json
import platform
from dataclasses import asdict, dataclass
from typing import Any

import psutil

from .db import connect, now_iso


@dataclass(frozen=True)
class InferenceTelemetry:
    created_at: str
    model: str
    provider: str
    prompt_tokens: int | None
    completion_tokens: int | None
    prompt_tokens_per_second: float | None
    generation_tokens_per_second: float | None
    latency_ms: float
    hardware: str


def _ensure_table() -> None:
    with connect() as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS model_telemetry (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                model TEXT NOT NULL,
                provider TEXT NOT NULL,
                prompt_tokens INTEGER,
                completion_tokens INTEGER,
                prompt_tokens_per_second REAL,
                generation_tokens_per_second REAL,
                latency_ms REAL NOT NULL,
                hardware TEXT NOT NULL,
                raw_json TEXT
            )
            """
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_model_telemetry_model_created "
            "ON model_telemetry(model, created_at DESC)"
        )


def hardware_signature() -> str:
    vm = psutil.virtual_memory()
    cpu = platform.processor() or platform.machine() or "unknown-cpu"
    ram_gb = round(vm.total / (1024 ** 3), 1)
    return f"{cpu} | {ram_gb} GB RAM | {platform.system()} {platform.release()}"


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _integer(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def extract_telemetry(model: str, body: dict[str, Any], elapsed_seconds: float,
                      provider: str = "openai-compatible") -> InferenceTelemetry:
    usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
    timings = body.get("timings") if isinstance(body.get("timings"), dict) else {}

    prompt_tokens = _integer(usage.get("prompt_tokens"))
    completion_tokens = _integer(usage.get("completion_tokens"))
    if prompt_tokens is None:
        prompt_tokens = _integer(timings.get("prompt_n"))
    if completion_tokens is None:
        completion_tokens = _integer(timings.get("predicted_n"))

    prompt_tps = _number(timings.get("prompt_per_second"))
    generation_tps = _number(timings.get("predicted_per_second"))

    if generation_tps is None and completion_tokens and elapsed_seconds > 0:
        generation_tps = completion_tokens / elapsed_seconds

    return InferenceTelemetry(
        created_at=now_iso(),
        model=model or "unknown",
        provider=provider,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        prompt_tokens_per_second=prompt_tps,
        generation_tokens_per_second=generation_tps,
        latency_ms=max(0.0, elapsed_seconds * 1000.0),
        hardware=hardware_signature(),
    )


def record_response(model: str, body: dict[str, Any], elapsed_seconds: float,
                    provider: str = "openai-compatible") -> dict[str, Any]:
    _ensure_table()
    item = extract_telemetry(model, body, elapsed_seconds, provider)
    with connect() as con:
        con.execute(
            """
            INSERT INTO model_telemetry(
                created_at, model, provider, prompt_tokens, completion_tokens,
                prompt_tokens_per_second, generation_tokens_per_second,
                latency_ms, hardware, raw_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                item.created_at, item.model, item.provider, item.prompt_tokens,
                item.completion_tokens, item.prompt_tokens_per_second,
                item.generation_tokens_per_second, item.latency_ms, item.hardware,
                json.dumps(body.get("timings") or {}, default=str)[:12000],
            ),
        )
    return asdict(item)


def latest(model: str | None = None) -> dict[str, Any] | None:
    _ensure_table()
    sql = (
        "SELECT created_at,model,provider,prompt_tokens,completion_tokens,"
        "prompt_tokens_per_second,generation_tokens_per_second,latency_ms,hardware "
        "FROM model_telemetry"
    )
    args: tuple[Any, ...] = ()
    if model:
        sql += " WHERE model=?"
        args = (model,)
    sql += " ORDER BY id DESC LIMIT 1"
    with connect() as con:
        row = con.execute(sql, args).fetchone()
    return dict(row) if row else None


def history(limit: int = 100, model: str | None = None) -> list[dict[str, Any]]:
    _ensure_table()
    limit = max(1, min(int(limit), 5000))
    sql = (
        "SELECT created_at,model,provider,prompt_tokens,completion_tokens,"
        "prompt_tokens_per_second,generation_tokens_per_second,latency_ms,hardware "
        "FROM model_telemetry"
    )
    args: list[Any] = []
    if model:
        sql += " WHERE model=?"
        args.append(model)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with connect() as con:
        rows = con.execute(sql, tuple(args)).fetchall()
    return [dict(row) for row in rows]


def model_summary() -> list[dict[str, Any]]:
    _ensure_table()
    with connect() as con:
        rows = con.execute(
            """
            SELECT model,
                   COUNT(*) AS samples,
                   AVG(prompt_tokens_per_second) AS avg_prompt_tps,
                   AVG(generation_tokens_per_second) AS avg_generation_tps,
                   MAX(created_at) AS last_used
            FROM model_telemetry
            GROUP BY model
            ORDER BY last_used DESC
            """
        ).fetchall()
    return [dict(row) for row in rows]
