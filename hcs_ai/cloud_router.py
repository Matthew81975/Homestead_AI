from __future__ import annotations

from collections import defaultdict
import os
import time

from .config import load_config
from .cloud_provider import OpenAICompatibleProvider, ProviderFailure

provider_factory = OpenAICompatibleProvider
_task_state = {}
_cooldowns = {}
_rotation = defaultdict(int)


def reset_runtime_state():
    _task_state.clear()
    _cooldowns.clear()
    _rotation.clear()


def _routes():
    cloud = load_config().get("cloud_ai", {})
    return [
        dict(route)
        for route in cloud.get("routes", [])
        if route.get("enabled", True)
    ]


def _healthy(route, now):
    return _cooldowns.get(route["id"], 0.0) <= now


def _weighted_order(routes):
    expanded = []
    for route in routes:
        expanded.extend([route] * max(1, int(route.get("weight", 1))))
    if not expanded:
        return []
    key = "|".join(sorted(r["id"] for r in routes))
    start = _rotation[key] % len(expanded)
    _rotation[key] += 1
    ordered = expanded[start:] + expanded[:start]
    seen = set()
    unique = []
    for route in ordered:
        if route["id"] not in seen:
            seen.add(route["id"])
            unique.append(route)
    return unique


def _ordered_candidates(routes, tier, current_model, now):
    same_model = [
        r for r in routes
        if r.get("tier") == tier
        and r.get("model") == current_model
        and _healthy(r, now)
    ]
    same_tier = [
        r for r in routes
        if r.get("tier") == tier
        and r.get("model") != current_model
        and _healthy(r, now)
    ]
    return _weighted_order(same_model) + _weighted_order(same_tier)


def _next_other_tier(routes, current_tier, now):
    candidates = [
        r for r in routes
        if r.get("tier") != current_tier and _healthy(r, now)
    ]
    ordered = _weighted_order(candidates)
    return ordered[0] if ordered else None


def approve_tier_change(task_id: str, tier: str) -> dict:
    state = _task_state.setdefault(task_id, {})
    state["tier"] = tier
    state.pop("pending_tier", None)
    state.pop("model", None)
    state.pop("provider", None)
    return {"ok": True, "task_id": task_id, "tier": tier}


def cloud_status() -> dict:
    cfg = load_config().get("cloud_ai", {})
    routes = _routes()
    now = time.monotonic()
    healthy = [r for r in routes if _healthy(r, now)]
    return {
        "enabled": bool(cfg.get("enabled")),
        "configured_routes": len(routes),
        "healthy_routes": len(healthy),
        "tiers": sorted({str(r.get("tier")) for r in routes if r.get("tier")}),
    }



def model_inventory(task_id: str | None = None) -> dict:
    routes = _routes()
    now = time.monotonic()
    state = _task_state.get(task_id or "", {})
    active_tier = state.get("tier")
    active_model = state.get("model")
    active_provider = state.get("provider")

    grouped = {}
    for route in routes:
        tier = str(route.get("tier") or "unassigned")
        model = str(route.get("model") or "")
        if not model:
            continue
        group = grouped.setdefault(
            (tier, model),
            {
                "tier": tier,
                "model": model,
                "configured_routes": 0,
                "healthy_routes": 0,
                "providers": [],
            },
        )
        healthy = _healthy(route, now)
        group["configured_routes"] += 1
        if healthy:
            group["healthy_routes"] += 1
        key_name = str(route.get("api_key_env") or "").strip()
        credential_configured = bool(key_name and os.environ.get(key_name))
        is_active = bool(
            tier == active_tier
            and model == active_model
            and route.get("provider") == active_provider
        )
        cooldown_until = _cooldowns.get(route["id"], 0.0)
        if cooldown_until == float("inf"):
            route_state = "unavailable"
            cooldown_remaining = None
        elif healthy:
            route_state = "active" if is_active else "ready"
            cooldown_remaining = 0.0
        else:
            route_state = "cooldown"
            cooldown_remaining = max(0.0, cooldown_until - now)
        group["providers"].append({
            "route_id": route["id"],
            "provider": route.get("provider") or route["id"],
            "state": route_state,
            "healthy": healthy,
            "credential_configured": credential_configured,
            "active": is_active,
            "cooldown_remaining_seconds": cooldown_remaining,
        })

    tier_names = sorted({tier for tier, _model in grouped})
    tiers = []
    for tier in tier_names:
        models = []
        healthy_in_tier = sum(
            item["healthy_routes"]
            for (item_tier, _model), item in grouped.items()
            if item_tier == tier
        )
        for (item_tier, _model), item in sorted(grouped.items()):
            if item_tier != tier:
                continue
            item["same_tier_failover_eligible"] = healthy_in_tier > 1
            item["active"] = bool(
                item["tier"] == active_tier and item["model"] == active_model
            )
            models.append(item)
        tiers.append({"tier": tier, "models": models})

    return {
        "task_id": task_id,
        "active_tier": active_tier,
        "active_model": active_model,
        "active_provider": active_provider,
        "tiers": tiers,
    }



def chat(task_id: str, messages: list[dict], tools: list[dict] | None = None) -> dict:
    cfg = load_config().get("cloud_ai", {})
    routes = _routes()
    if not cfg.get("enabled") or not routes:
        raise RuntimeError("Cloud AI provider pool is not configured.")

    state = _task_state.setdefault(task_id, {})
    tier = state.get("tier") or str(cfg.get("default_tier") or "high")
    current_model = state.get("model")

    if not current_model:
        first = next((r for r in routes if r.get("tier") == tier), None)
        if not first:
            raise RuntimeError(f"No cloud route is configured for capability tier: {tier}")
        current_model = first["model"]
        state["model"] = current_model
        state["tier"] = tier

    now = time.monotonic()
    cooldown_default = float(cfg.get("cooldown_seconds", 60))
    errors = []

    for route in _ordered_candidates(routes, tier, current_model, now):
        try:
            result = provider_factory(route).complete(messages, tools)
            state.update({
                "tier": route["tier"],
                "provider": route["provider"],
                "model": route["model"],
            })
            return {
                **result,
                "tier": route["tier"],
                "approval_required": False,
            }
        except ProviderFailure as exc:
            errors.append({
                "route": route["id"],
                "kind": exc.kind,
                "message": str(exc),
            })
            if exc.kind == "auth":
                _cooldowns[route["id"]] = float("inf")
                continue
            if exc.kind in ("rate_limit", "capacity", "timeout", "connectivity"):
                _cooldowns[route["id"]] = now + (
                    exc.retry_after_seconds or cooldown_default
                )
                continue
            raise

    replacement = _next_other_tier(routes, tier, now)
    if replacement:
        state["pending_tier"] = replacement["tier"]
        return {
            "approval_required": True,
            "task_id": task_id,
            "current_tier": tier,
            "proposed_tier": replacement["tier"],
            "provider": replacement["provider"],
            "model": replacement["model"],
            "message": (
                f"No route remains in the {tier} capability tier. "
                f"Continue with {replacement['model']} in the "
                f"{replacement['tier']} tier?"
            ),
            "errors": errors,
        }

    raise RuntimeError("No healthy cloud AI route is currently available.")
