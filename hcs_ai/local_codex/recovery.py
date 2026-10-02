from __future__ import annotations

from typing import Iterable


GENERAL_STRATEGIES = (
    "use_cached_results",
    "change_action",
    "reduce_context",
    "subdivide_task",
    "checkpoint_and_resume",
    "replan_from_goal",
    "alternate_capability",
    "alternate_provider_or_local",
)

CATEGORY_STRATEGIES = {
    "model_format": (
        "repair_action_format",
        "reduce_context",
        "change_action",
        "subdivide_task",
        "alternate_provider_or_local",
        "replan_from_goal",
    ),
    "provider": (
        "alternate_provider_or_local",
        "retry_transient",
        "reduce_context",
        "checkpoint_and_resume",
        "subdivide_task",
        "replan_from_goal",
    ),
    "rate_limit": (
        "alternate_provider_or_local",
        "checkpoint_and_resume",
        "subdivide_task",
        "reduce_context",
        "retry_transient",
        "replan_from_goal",
    ),
    "context": (
        "reduce_context",
        "subdivide_task",
        "use_cached_results",
        "checkpoint_and_resume",
        "alternate_provider_or_local",
        "replan_from_goal",
    ),
    "stagnation": (
        "use_cached_results",
        "change_action",
        "subdivide_task",
        "replan_from_goal",
        "alternate_capability",
        "checkpoint_and_resume",
    ),
    "missing_path": (
        "use_cached_results",
        "change_action",
        "alternate_capability",
        "replan_from_goal",
    ),
    "permission": (
        "alternate_capability",
        "replan_from_goal",
        "checkpoint_and_resume",
    ),
    "test_failure": (
        "inspect_failure",
        "change_action",
        "subdivide_task",
        "replan_from_goal",
    ),
    "completion_gate": (
        "run_required_verification",
        "use_cached_results",
        "change_action",
    ),
}


def classify_failure(reason: str, *, exception: Exception | None = None) -> str:
    category = getattr(exception, "category", None)
    if isinstance(category, str) and category:
        if category in {"rate_limit", "budget", "transient_budget"}:
            return "rate_limit"
        if category in {"auth", "client_block", "transient", "provider"}:
            return "provider"

    text = str(reason).lower()
    if any(term in text for term in ("context length", "context window", "too many tokens", "token limit", "prompt too long")):
        return "context"
    if any(term in text for term in ("model response must be text", "action field is required", "invalid json", "json")):
        return "model_format"
    if "path not found" in text or "file not found" in text:
        return "missing_path"
    if "permission" in text or "approval required" in text:
        return "permission"
    if "repeated" in text or "same successful inspection" in text:
        return "stagnation"
    if "finish rejected" in text:
        return "completion_gate"
    if "test" in text and any(term in text for term in ("failed", "failure", "error")):
        return "test_failure"
    if any(term in text for term in ("timed out", "timeout", "provider", "lm studio error", "http 429", "rate limit")):
        return "provider"
    return "generic"


def strategy_order(category: str) -> tuple[str, ...]:
    preferred = CATEGORY_STRATEGIES.get(category, ())
    result: list[str] = []
    for item in (*preferred, *GENERAL_STRATEGIES):
        if item not in result:
            result.append(item)
    return tuple(result)


def next_strategy(category: str, attempted: Iterable[str]) -> str | None:
    used = set(attempted)
    for strategy in strategy_order(category):
        if strategy not in used:
            return strategy
    return None


STRATEGY_INSTRUCTIONS = {
    "repair_action_format": "Return one valid allowed JSON action only. Repair the previous malformed action without adding prose.",
    "use_cached_results": "Do not reread information already present in recent_steps or retained observations. Act on the evidence already collected.",
    "change_action": "Choose a materially different action from the one that stalled. Do not repeat an equivalent inspection with only cosmetic changes.",
    "reduce_context": "Reduce working context: use targeted search/read ranges and only the files needed for the immediate leaf of work.",
    "subdivide_task": "Split the current work into a smaller executable leaf that can be completed and verified independently; do that leaf now, then continue with the remaining leaves.",
    "checkpoint_and_resume": "Preserve completed work and resume from the last successful checkpoint instead of restarting the task.",
    "replan_from_goal": "Re-plan from the original task goal using the evidence already collected. Pick a different implementation route.",
    "alternate_capability": "Use an equivalent available capability/tool route rather than repeating the unavailable or failing one.",
    "alternate_provider_or_local": "Use provider fallback. Prefer a free cloud lane; if unavailable, use the immediate local LM Studio fallback rather than waiting.",
    "retry_transient": "Retry only if the failure is transient; otherwise immediately choose another provider/capability.",
    "inspect_failure": "Inspect the concrete failing test/build output, make the smallest corrective change, then rerun the focused verification.",
    "run_required_verification": "Complete the missing verification gate (diff/tests as applicable) instead of trying to finish again.",
}


def recovery_instruction(*, category: str, strategy: str, reason: str, cycle: int, maximum: int) -> str:
    directive = STRATEGY_INSTRUCTIONS.get(strategy, STRATEGY_INSTRUCTIONS["replan_from_goal"])
    return (
        f"AUTONOMOUS RECOVERY {cycle}/{maximum}. "
        f"Failure category: {category}. Strategy: {strategy}. "
        f"Reason: {reason}. {directive} "
        "Do not ask the user to perform routine development work. "
        "Only escalate when a credential, permission, destructive decision, or genuinely human-only choice is required."
    )
