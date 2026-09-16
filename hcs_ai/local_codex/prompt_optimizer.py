from __future__ import annotations

import json


class PromptOptimizer:
    """Compact cloud-bound prompts without changing system instructions."""

    def optimize(self, messages: list[dict[str, str]]) -> list[dict[str, str]]:
        optimized: list[dict[str, str]] = []
        for message in messages:
            item = dict(message)
            if item.get("role") == "system":
                optimized.append(item)
                continue

            content = item.get("content", "")
            if isinstance(content, str):
                stripped = content.strip()
                try:
                    parsed = json.loads(stripped)
                except (json.JSONDecodeError, TypeError):
                    lines = [line.rstrip() for line in stripped.splitlines()]
                    compact: list[str] = []
                    blank = False
                    for line in lines:
                        if not line.strip():
                            if compact and not blank:
                                compact.append("")
                            blank = True
                            continue
                        compact.append(line.strip() if line != line.lstrip() else line)
                        blank = False
                    while compact and compact[-1] == "":
                        compact.pop()
                    item["content"] = "\n".join(compact)
                else:
                    item["content"] = json.dumps(
                        parsed,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
            optimized.append(item)
        return optimized
