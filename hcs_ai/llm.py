import json
import time

import httpx

from .config import load_config, llm_config
from .knowledge import search as kb_search
from .telemetry import record_response
from .tools import public_tool_specs, call_tool


def _response_error(response):
    try:
        body = response.json()
        detail = body.get("error", body) if isinstance(body, dict) else body
        if isinstance(detail, dict):
            detail = detail.get("message") or detail.get("detail") or detail
        return str(detail)
    except Exception:
        return response.text.strip() or response.reason_phrase


def _post_completion(client, url, payload, allow_tool_fallback=True):
    """Call the local model server, retrying without tools when needed."""
    try:
        response = client.post(url, json=payload)
    except httpx.ConnectError as exc:
        raise RuntimeError(
            "Cannot connect to the HCS local model engine. Check Internal AI in the System tab and verify "
            f"the address in config.json ({url})."
        ) from exc
    except httpx.TimeoutException as exc:
        raise RuntimeError("The local model engine timed out while generating a response.") from exc

    if response.is_success:
        return response, bool(payload.get("tools"))

    if allow_tool_fallback and response.status_code in (400, 422) and payload.get("tools"):
        basic_payload = {k: v for k, v in payload.items() if k not in ("tools", "tool_choice")}
        retry = client.post(url, json=basic_payload)
        if retry.is_success:
            return retry, False
        raise RuntimeError(
            f"The local model engine rejected both tool mode ({_response_error(response)}) and "
            f"ordinary chat ({_response_error(retry)})."
        )

    raise RuntimeError(f"The local model engine returned HTTP {response.status_code}: {_response_error(response)}")


def _tool_definitions():
    defs = []
    for t in public_tool_specs():
        props, required = {}, []
        for k, desc in t["schema"].items():
            typ = "string"
            if "integer" in desc:
                typ = "integer"
            elif "boolean" in desc:
                typ = "boolean"
            props[k] = {"type": typ, "description": desc}
            if "optional" not in desc:
                required.append(k)
        defs.append({
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": {"type": "object", "properties": props, "required": required}
            }
        })
    return defs


def _resolve_model(client, llm, override=None):
    if override and override != "auto":
        return override
    configured = llm.get("model", "auto")
    if configured and configured != "auto":
        return configured
    models_url = llm["base_url"].rstrip("/") + "/models"
    r = client.get(models_url)
    r.raise_for_status()
    data = r.json().get("data", [])
    if not data:
        raise RuntimeError("No model is loaded in the local model server.")
    return data[0]["id"]


def chat(user_message: str, history=None, use_kb: bool = True, model: str | None = None):
    total_started = time.perf_counter()
    cfg = load_config()
    llm = llm_config()

    kb_started = time.perf_counter()
    context = ""
    hits = []
    if use_kb:
        hits = kb_search(user_message, limit=4)
        if hits:
            context = "\n\nLOCAL KNOWLEDGE CONTEXT:\n" + "\n\n".join(
                f"[{h['source']} #{h['chunk_index']}]\n{h['text']}" for h in hits
            )
    kb_seconds = time.perf_counter() - kb_started

    messages = [{"role": "system", "content": cfg["app"]["system_prompt"] + context}]
    if history:
        messages.extend(history[-12:])
    messages.append({"role": "user", "content": user_message})
    url = llm["base_url"].rstrip("/") + "/chat/completions"
    all_results = []
    round_timings = []

    def finish(text, tool_mode):
        return {
            "text": text,
            "tool_results": all_results,
            "tool_mode": tool_mode,
            "backend_timings": {
                "total_seconds": time.perf_counter() - total_started,
                "kb_seconds": kb_seconds,
                "model_resolve_seconds": model_resolve_seconds,
                "tool_definition_seconds": tool_definition_seconds,
                "rounds": round_timings,
            },
            "prompt_meta": {
                "message_count": len(messages),
                "history_messages": min(len(history or []), 12),
                "system_chars": len(cfg["app"]["system_prompt"]),
                "kb_context_chars": len(context),
                "kb_hits": len(hits),
                "user_chars": len(user_message),
            },
        }

    with httpx.Client(timeout=llm["timeout_seconds"]) as client:
        tools_enabled = True

        resolve_started = time.perf_counter()
        resolved_model = _resolve_model(client, llm, model)
        model_resolve_seconds = time.perf_counter() - resolve_started

        tools_started = time.perf_counter()
        tool_definitions = _tool_definitions()
        tool_definition_seconds = time.perf_counter() - tools_started

        payload = {
            "model": resolved_model,
            "messages": messages,
            "temperature": llm["temperature"],
            "tools": tool_definitions,
            "tool_choice": "auto",
        }
        for _round in range(5):
            payload["messages"] = messages
            started = time.perf_counter()
            r, tools_enabled = _post_completion(
                client, url, payload, allow_tool_fallback=tools_enabled
            )
            completion_seconds = time.perf_counter() - started
            body = r.json()
            try:
                record_response(resolved_model, body, completion_seconds)
            except Exception:
                pass

            msg = body["choices"][0]["message"]
            tool_calls = msg.get("tool_calls") or []
            allowed_tool_names = {item["name"] for item in public_tool_specs()}
            valid_tool_calls = [
                tc for tc in tool_calls
                if isinstance(tc, dict)
                and isinstance(tc.get("function"), dict)
                and tc["function"].get("name") in allowed_tool_names
            ]
            invalid_tool_calls = [
                tc for tc in tool_calls
                if tc not in valid_tool_calls
            ]
            round_info = {
                "round": _round + 1,
                "completion_seconds": completion_seconds,
                "tool_calls": len(tool_calls),
                "valid_tool_calls": len(valid_tool_calls),
                "invalid_tool_calls": [
                    (tc.get("function") or {}).get("name")
                    for tc in invalid_tool_calls
                    if isinstance(tc, dict)
                ],
                "tools_enabled": tools_enabled,
                "usage": body.get("usage") if isinstance(body.get("usage"), dict) else {},
                "provider_timings": body.get("timings") if isinstance(body.get("timings"), dict) else {},
            }
            round_timings.append(round_info)

            if not tool_calls:
                return finish(msg.get("content", ""), tools_enabled)

            # Never execute undeclared/unknown tool names. Some small local models
            # occasionally emit a spurious tool call alongside perfectly usable text.
            # In that case, keep the text and avoid a needless second inference round.
            if not valid_tool_calls:
                content = msg.get("content", "")
                if content:
                    return finish(content, tools_enabled)
                return finish(
                    "The model produced an invalid tool call and no usable text response.",
                    tools_enabled,
                )

            messages.append(msg)
            tool_started = time.perf_counter()
            for tc in valid_tool_calls:
                name = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"].get("arguments") or "{}")
                    result = call_tool(name, args)
                    result_text, ok = json.dumps(result, default=str), True
                except Exception as e:
                    result_text, ok = json.dumps({"error": str(e)}), False
                all_results.append({"tool": name, "ok": ok, "result": result_text[:8000]})
                messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result_text[:24000]})
            round_info["tool_seconds"] = time.perf_counter() - tool_started

        return finish("I reached the tool-use round limit before completing the answer.", tools_enabled)
