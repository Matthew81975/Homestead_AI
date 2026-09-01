from fastapi.testclient import TestClient
import hcs_ai.server as server


client = TestClient(server.app)


def test_offline_chat_keeps_local_path(monkeypatch):
    monkeypatch.setattr(server, "audit", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "_ai_mode_status", lambda: {"effective_mode": "offline"})
    monkeypatch.setattr(server, "llm_chat", lambda message, history, use_kb: {"text": "local"})
    response = client.post("/chat", json={"message": "hi", "task_id": "t1"})
    assert response.status_code == 200
    assert response.json()["text"] == "local"


def test_live_chat_uses_cloud_router(monkeypatch):
    monkeypatch.setattr(server, "audit", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "_ai_mode_status", lambda: {"effective_mode": "live"})
    monkeypatch.setattr(
        server.cloud_router,
        "chat",
        lambda task_id, messages, tools=None: {
            "text": "cloud",
            "provider": "p1",
            "model": "m1",
            "tier": "high",
            "approval_required": False,
        },
    )
    response = client.post("/chat", json={"message": "hi", "history": [], "use_kb": False, "task_id": "t2"})
    body = response.json()
    assert body["text"] == "cloud"
    assert body["provider"] == "p1"
    assert body["tier"] == "high"


def test_tier_approval_endpoint(monkeypatch):
    monkeypatch.setattr(
        server.cloud_router,
        "approve_tier_change",
        lambda task_id, tier: {"ok": True, "task_id": task_id, "tier": tier},
    )
    response = client.post("/ai/approve-tier", json={"task_id": "t3", "tier": "medium"})
    assert response.status_code == 200
    assert response.json()["tier"] == "medium"


def test_ai_models_endpoint_is_secret_free(monkeypatch):
    monkeypatch.setattr(
        server.cloud_router,
        "model_inventory",
        lambda task_id=None: {
            "tiers": [{
                "tier": "high",
                "models": [{
                    "model": "m1",
                    "providers": [{
                        "provider": "p1",
                        "credential_configured": True,
                    }],
                }],
            }],
        },
    )
    response = client.get("/ai/models?task_id=t1")
    assert response.status_code == 200
    assert "api_key" not in response.text.lower()


def test_live_chat_audits_each_route_event(monkeypatch):
    events = []
    monkeypatch.setattr(server, "audit", lambda kind, detail: events.append((kind, detail)))
    monkeypatch.setattr(server, "_ai_mode_status", lambda: {"effective_mode": "live"})
    monkeypatch.setattr(
        server.cloud_router,
        "chat",
        lambda task_id, messages, tools=None: {
            "text": "cloud",
            "provider": "p2",
            "model": "m1",
            "tier": "high",
            "approval_required": False,
            "route_events": [
                {
                    "route": "a",
                    "provider": "p1",
                    "model": "m1",
                    "tier": "high",
                    "outcome": "failure",
                    "reason": "rate_limit",
                },
                {
                    "route": "b",
                    "provider": "p2",
                    "model": "m1",
                    "tier": "high",
                    "outcome": "success",
                    "reason": "completed",
                },
            ],
        },
    )
    response = client.post("/chat", json={"message": "hi", "history": [], "use_kb": False, "task_id": "t-events"})
    assert response.status_code == 200
    route_audits = [detail for kind, detail in events if kind == "cloud_route_event"]
    assert len(route_audits) == 2
    assert "rate_limit" in route_audits[0]

def test_live_chat_includes_active_kb_context(monkeypatch):
    captured = {}
    monkeypatch.setattr(server, "audit", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "_ai_mode_status", lambda: {"effective_mode": "live"})
    monkeypatch.setattr(
        server,
        "kb_search",
        lambda query, limit=4: [{
            "source": "orchard.md",
            "chunk_index": 2,
            "text": "A node is stale after 3.5 reporting intervals.",
            "score": 1.0,
        }],
    )

    def fake_cloud_chat(task_id, messages, tools=None):
        captured["messages"] = messages
        return {
            "text": "grounded",
            "provider": "p1",
            "model": "m1",
            "tier": "high",
            "approval_required": False,
        }

    monkeypatch.setattr(server.cloud_router, "chat", fake_cloud_chat)
    response = client.post(
        "/chat",
        json={
            "message": "When is a node stale?",
            "use_kb": True,
            "task_id": "t-kb",
        },
    )

    assert response.status_code == 200
    system_message = captured["messages"][0]["content"]
    assert "LOCAL KNOWLEDGE CONTEXT" in system_message
    assert "[orchard.md #2]" in system_message
    assert "3.5 reporting intervals" in system_message


def test_live_chat_skips_kb_search_when_disabled(monkeypatch):
    captured = {}
    monkeypatch.setattr(server, "audit", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "_ai_mode_status", lambda: {"effective_mode": "live"})
    monkeypatch.setattr(
        server,
        "kb_search",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("KB search must not run")
        ),
    )

    def fake_cloud_chat(task_id, messages, tools=None):
        captured["messages"] = messages
        return {
            "text": "ungrounded",
            "provider": "p1",
            "model": "m1",
            "tier": "high",
            "approval_required": False,
        }

    monkeypatch.setattr(server.cloud_router, "chat", fake_cloud_chat)
    response = client.post(
        "/chat",
        json={
            "message": "hello",
            "use_kb": False,
            "task_id": "t-no-kb",
        },
    )

    assert response.status_code == 200
    assert "LOCAL KNOWLEDGE CONTEXT" not in captured["messages"][0]["content"]

