import json
import httpx
from .config import load_config, llm_config
from .knowledge import search as kb_search
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

def _resolve_model(client, llm):
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

def chat(user_message: str, history=None, use_kb: bool=True):
    cfg = load_config()
    llm = llm_config()
    context = ""
    if use_kb:
        hits = kb_search(user_message, limit=4)
        if hits:
            context = "\n\nLOCAL KNOWLEDGE CONTEXT:\n" + "\n\n".join(
                f"[{h['source']} #{h['chunk_index']}]\n{h['text']}" for h in hits
            )
    messages = [{"role": "system", "content": cfg["app"]["system_prompt"] + context}]
    if history:
        messages.extend(history[-12:])
    messages.append({"role": "user", "content": user_message})
    url = llm["base_url"].rstrip("/") + "/chat/completions"
    all_results=[]
    with httpx.Client(timeout=llm["timeout_seconds"]) as client:
        tools_enabled = True
        payload = {
            "model": _resolve_model(client, llm), "messages": messages,
            "temperature": llm["temperature"], "tools": _tool_definitions(), "tool_choice": "auto"
        }
        # Multiple rounds let the model do: search HKR -> read source -> research HKR -> read -> answer.
        for _round in range(5):
            payload["messages"] = messages
            r, tools_enabled = _post_completion(
                client, url, payload, allow_tool_fallback=tools_enabled
            )
            msg = r.json()["choices"][0]["message"]
            tool_calls = msg.get("tool_calls") or []
            if not tool_calls:
                return {
                    "text": msg.get("content", ""),
                    "tool_results": all_results,
                    "tool_mode": tools_enabled,
                }
            messages.append(msg)
            for tc in tool_calls:
                name = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"].get("arguments") or "{}")
                    result = call_tool(name, args)
                    result_text, ok = json.dumps(result, default=str), True
                except Exception as e:
                    result_text, ok = json.dumps({"error": str(e)}), False
                all_results.append({"tool": name, "ok": ok, "result": result_text[:8000]})
                messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result_text[:24000]})
        return {"text": "I reached the tool-use round limit before completing the answer.", "tool_results": all_results}
